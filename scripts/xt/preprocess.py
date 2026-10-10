"""Bildförbehandling för dåliga scans (journaler, fax, fotokopior, mobilfoton):
rotation (Tesseract OSD), uträtning, belysningsutjämning och binarisering."""
from __future__ import annotations

import re

import numpy as np

try:
    import cv2
    HAVE_CV = True
except ImportError:  # pragma: no cover
    HAVE_CV = False


def to_gray(img) -> np.ndarray:
    """PIL-bild -> uint8 gråskala."""
    a = np.asarray(img.convert("L") if img.mode != "L" else img)
    return a.copy()


def ink_ratio(gray: np.ndarray) -> float:
    """Andel 'bläck'-pixlar – under ~0,2 % är sidan i praktiken tom."""
    if not HAVE_CV:
        return float((gray < 128).mean())
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    _, b = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Otsu på en helvit sida hittar "bläck" i papperstexturen -> kräv kontrast också
    if int(small.max()) - int(small.min()) < 40:
        return 0.0
    return float((b > 0).mean())


def osd_rotation(gray: np.ndarray) -> int:
    """Grader att rotera (0/90/180/270) enligt Tesseract OSD; 0 vid osäkerhet."""
    try:
        import pytesseract
        from PIL import Image

        h, w = gray.shape
        k = 1600 / max(h, w)
        small = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if (HAVE_CV and k < 1) else gray
        out = pytesseract.image_to_osd(Image.fromarray(small), config="--psm 0 -c min_characters_to_try=10")
        rot = int(re.search(r"Rotate:\s+(\d+)", out).group(1))
        conf = float(re.search(r"Orientation confidence:\s+([\d.]+)", out).group(1))
        return rot if rot and conf >= 2.0 else 0
    except Exception:
        return 0


def rotate90(gray: np.ndarray, deg: int) -> np.ndarray:
    if not deg:
        return gray
    k = {90: -1, 180: 2, 270: 1}[deg]  # OSD 'Rotate' = medurs
    return np.ascontiguousarray(np.rot90(gray, k))


def estimate_skew(gray: np.ndarray, max_angle: float = 6.0) -> float:
    """Liten snedvridning via projektionsprofil: den vinkel där radsummorna har
    störst varians (textrader exakt horisontella) vinner. Tål bilder/stämplar
    bättre än minAreaRect."""
    if not HAVE_CV:
        return 0.0
    h, w = gray.shape
    k = 1000 / max(h, w)
    small = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else gray
    # Belysningsnormalisera + beskär kanterna FÖRST: en grå rotationskant eller
    # skuggradient dominerar annars radsummorna och ger fel vinkel (uppmätt:
    # -6,5° i stället för -3,2° på testfallet scan_sned_ojamn).
    small = normalize_illumination(small)
    sh0, sw0 = small.shape
    my, mx = int(sh0 * 0.05), int(sw0 * 0.05)
    small = small[my:sh0 - my, mx:sw0 - mx]
    _, b = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # bara tecken-stora komponenter (stämplar, linjer och bilder bort)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(b, 8)
    if n > 1:
        hs = stats[1:, cv2.CC_STAT_HEIGHT]
        med = float(np.median(hs[hs > 2])) if (hs > 2).any() else 0
        if med:
            keep = np.zeros(n, bool)
            keep[1:] = (hs >= 0.4 * med) & (hs <= 2.5 * med) & (stats[1:, cv2.CC_STAT_WIDTH] <= 4 * med)
            b = np.where(keep[lab], 255, 0).astype(np.uint8)
    sh, sw = b.shape
    center = (sw / 2, sh / 2)

    def score(a):
        m = cv2.getRotationMatrix2D(center, a, 1.0)
        r = cv2.warpAffine(b, m, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        return float(np.var(r.sum(axis=1, dtype=np.float64)))

    best = max(np.arange(-max_angle, max_angle + 0.01, 0.5), key=score)
    best = max(np.arange(best - 0.5, best + 0.51, 0.1), key=score)
    return float(round(best, 2)) if abs(best) >= 0.15 else 0.0


def rotate_small(gray: np.ndarray, angle: float) -> np.ndarray:
    if not angle or not HAVE_CV:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def normalize_illumination(gray: np.ndarray) -> np.ndarray:
    """Dela bort bakgrunden (skuggor, gulnat papper, ojämn kopiering) så att
    texten får jämn kontrast över hela sidan."""
    if not HAVE_CV:
        return gray
    h, w = gray.shape
    k = max(15, (min(h, w) // 30) | 1)
    bg = cv2.medianBlur(cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8)), k if k < 255 else 255)
    norm = cv2.divide(gray, bg, scale=255)
    return cv2.normalize(norm, None, 0, 255, cv2.NORM_MINMAX)


def binarize_adaptive(gray: np.ndarray) -> np.ndarray:
    """Lokal tröskel – bäst för bleka eller fläckiga kopior."""
    if not HAVE_CV:
        return gray
    h, w = gray.shape
    block = max(15, (min(h, w) // 60) | 1)
    den = cv2.medianBlur(gray, 3)
    return cv2.adaptiveThreshold(den, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 15)


def upscale(gray: np.ndarray, f: float = 1.5) -> np.ndarray:
    if not HAVE_CV:
        return gray
    return cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)


def char_blobs(norm: np.ndarray) -> np.ndarray:
    """Bokstavsstora bläckkomponenter (x, y, w, h) – underlaget för täcknings-
    kontrollen: Tesseract kan tappa hela rader med HÖG konfidens på resten."""
    if not HAVE_CV:
        return np.zeros((0, 4), int)
    _, b = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, _lab, stats, _ = cv2.connectedComponentsWithStats(b, 8)
    if n <= 1:
        return np.zeros((0, 4), int)
    s = stats[1:]
    hs, ws, area = s[:, cv2.CC_STAT_HEIGHT], s[:, cv2.CC_STAT_WIDTH], s[:, cv2.CC_STAT_AREA]
    cand = (hs >= 8) & (area >= 15)
    if not cand.any():
        return np.zeros((0, 4), int)
    med = float(np.median(hs[cand]))
    keep = cand & (hs >= 0.5 * med) & (hs <= 2.2 * med) & (ws <= 3 * med) & (area < 0.9 * hs * ws + 1)
    return _in_text_lines(s[keep][:, :4], norm.shape)


def _in_text_lines(boxes: np.ndarray, shape) -> np.ndarray:
    """Behåll bara blobbar som ingår i en textrad: smeta ut dem horisontellt,
    och godkänn radkomponenter som är breda (>= 4 teckenhöjder), låga och har
    >= 4 blobbar med likartad höjd. Stjärnfält, skrafferingar och lösa prickar
    i illustrationer faller bort (uppmätt på Darkstryder/Kathol Rift s.41)."""
    if len(boxes) == 0:
        return boxes
    med = float(np.median(boxes[:, 3]))
    mask = np.zeros(shape, np.uint8)
    for x, y, w, h in boxes:
        mask[y:y + h, x:x + w] = 255
    k = max(3, int(med * 1.1))
    lines = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(lines, 8)
    cx = np.clip((boxes[:, 0] + boxes[:, 2] // 2).astype(int), 0, shape[1] - 1)
    cy = np.clip((boxes[:, 1] + boxes[:, 3] // 2).astype(int), 0, shape[0] - 1)
    owner = lab[cy, cx]
    ok_line = np.zeros(n, bool)
    for li in range(1, n):
        lw, lh = st[li, cv2.CC_STAT_WIDTH], st[li, cv2.CC_STAT_HEIGHT]
        members = boxes[owner == li]
        if len(members) < 4 or lw < 4 * med or lh > 3.0 * med:
            continue
        hh = members[:, 3]
        bottoms = members[:, 1] + hh
        if np.std(hh) / (np.mean(hh) + 1e-6) < 0.45 and np.std(bottoms) < 0.6 * med:
            ok_line[li] = True
    return boxes[ok_line[owner]]


def coverage(words: list[dict], blobs: np.ndarray) -> float:
    """Andel tecken-blobbar vars mittpunkt ligger i (lätt förstorad) ordruta."""
    if len(blobs) == 0:
        return 1.0
    if not words:
        return 0.0
    wb = np.array([[w["x"], w["y"], w.get("w", w["h"]), w["h"]] for w in words], float)
    pad = 0.35 * wb[:, 3:4]
    x0, y0 = wb[:, 0:1] - pad, wb[:, 1:2] - pad
    x1, y1 = wb[:, 0:1] + wb[:, 2:3] + pad, wb[:, 1:2] + wb[:, 3:4] + pad
    cx = (blobs[:, 0] + blobs[:, 2] / 2)[None, :]
    cy = (blobs[:, 1] + blobs[:, 3] / 2)[None, :]
    inside = ((cx >= x0) & (cx <= x1) & (cy >= y0) & (cy <= y1)).any(axis=0)
    return float(inside.mean())


TEXTURE_DENSITY = 0.20


def unread_lines(norm: np.ndarray, words: list[dict]) -> int:
    """Antal radliknande bläckstråk som Tesseract INTE läst – typiskt handskrift
    (blyerts på förtryckta formulär: Tesseract läser formuläret med hög
    konfidens och hoppar tyst över allt handskrivet; uppmätt på brittisk
    krigsdagbok 1915). Formulärlinjer, prickar och stora bildytor räknas inte."""
    if not HAVE_CV:
        return 0
    h, w = norm.shape
    den = cv2.medianBlur(norm, 3)
    ink = cv2.adaptiveThreshold(den, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
                                max(15, (min(h, w) // 60) | 1), 18)
    # Texturytor (rastrerade/skrafferade illustrationer) har hög lokal
    # bläcktäthet; handskrift och maskinskrift ligger på ~5-15 %. Mäts FÖRE
    # ordmaskningen så att tätheten avser hela sidan.
    # Beräknas i 1/4-upplösning (en float32-buffert i full storlek per tråd
    # drev toppminnet över 1 GB med 8 trådar) och skalas upp till en mask.
    small = cv2.resize(ink, (max(1, w // 4), max(1, h // 4)), interpolation=cv2.INTER_AREA)
    kk = max(5, int(0.04 * min(h, w) / 4))
    dens = cv2.blur(small, (kk, kk))  # uint8: 0-255 ~ andel bläck
    texture = cv2.resize((dens > int(TEXTURE_DENSITY * 255)).astype(np.uint8), (w, h),
                         interpolation=cv2.INTER_NEAREST).astype(bool)
    for wd in words:  # maska bort allt som lästs
        pad = int(0.4 * wd["h"])
        x0, y0 = max(0, wd["x"] - pad), max(0, wd["y"] - pad)
        ink[y0:wd["y"] + wd["h"] + pad, x0:wd["x"] + wd.get("w", wd["h"]) + pad] = 0
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink, 8)
    if n <= 1:
        return 0
    cw, ch, ca = st[:, cv2.CC_STAT_WIDTH], st[:, cv2.CC_STAT_HEIGHT], st[:, cv2.CC_STAT_AREA]
    # Bildregioner: stora OCH täta komponenter (illustrationer). Formulärramar
    # och tabellrutnät är stora men glesa (fyllnad < ~4 %) och får vara kvar,
    # annars maskas handskriften i formulärets rutor bort.
    fill = ca / np.maximum(1, cw * ch)
    big = ((ch > 0.05 * h) | (cw > 0.3 * w)) & (fill > 0.045)
    big[0] = False
    for j in np.where(big)[0]:
        x, y = st[j, cv2.CC_STAT_LEFT], st[j, cv2.CC_STAT_TOP]
        m = int(0.01 * max(h, w))
        lab[max(0, y - m):y + ch[j] + m, max(0, x - m):x + cw[j] + m] = 0
    keep = ((ca >= 0.00001 * h * w) & (ch >= 0.004 * h) & (ch <= 0.05 * h)
            & (cw <= 0.5 * w) & (cw < 15 * ch) & (ch < 15 * cw))  # inga linjer/ramar
    keep[0] = False
    strokes = keep[lab] & ~texture
    # Radband i projektionsprofilen: rader där olästa streck täcker en
    # betydande bredd. Tål att handskriftens över-/underhäng flyter ihop
    # raderna till ett enda sammanhängande block (vilket komponentanalys inte gör).
    cols_hit = np.zeros(h)
    xs = np.where(strokes.any(axis=0))[0]
    if len(xs) == 0:
        return 0
    row_cover = np.zeros(h)
    step = max(1, w // 400)
    sub = strokes[:, ::step]
    # andel av radens bredd (i 1/400-delar) som har streck, utjämnad vertikalt
    row_cover = sub.mean(axis=1)
    kern = max(3, int(0.006 * h))
    row_cover = np.convolve(row_cover, np.ones(kern) / kern, mode="same")
    active = row_cover > 0.04
    bands, run = 0, 0
    for a in np.append(active, False):
        if a:
            run += 1
        else:
            if 0.006 * h <= run <= 0.08 * h:
                bands += 1
            elif run > 0.08 * h:  # ihopflutna rader: uppskatta antal radhöjder
                bands += int(run / (0.025 * h))
            run = 0
    del cols_hit
    return bands


def _trim_text_edges(box, text_blocks, step_frac: float = 0.015, max_share: float = 0.15):
    """Tälj bort kantremsor som mest består av text. Bakgrundskonst bakom en
    textspalt kan hänga ihop med en inramad bild (WEG40122 s.61: serierutans ram
    nuddar konsten i högerspalten) – då blir kandidaten hela sidan. Remsan med
    störst textandel skärs bort tills ingen kant har mer än max_share text."""
    x0, y0, x1, y1 = box
    H, W = text_blocks.shape
    sx, sy = max(2, int(step_frac * W)), max(2, int(step_frac * H))
    for _ in range(400):
        if x1 - x0 <= 2 * sx or y1 - y0 <= 2 * sy:
            break
        edges = {
            "l": text_blocks[y0:y1, x0:x0 + sx], "r": text_blocks[y0:y1, x1 - sx:x1],
            "t": text_blocks[y0:y0 + sy, x0:x1], "b": text_blocks[y1 - sy:y1, x0:x1],
        }
        share = {k: (v > 0).mean() for k, v in edges.items()}
        side = max(share, key=share.get)
        if share[side] <= max_share:
            break
        if side == "l":
            x0 += sx
        elif side == "r":
            x1 -= sx
        elif side == "t":
            y0 += sy
        else:
            y1 -= sy
    return [x0, y0, x1, y1]


def _split_on_text(box, text_blocks, min_frac: float = 0.06, share: float = 0.3, depth: int = 0):
    """Dela en kandidatruta längs kolumner/rader som domineras av text. En
    textspalt MITT i en ruta (konst på båda sidor) är en skiljelinje, inte en del
    av bilden. Rekursivt: först kolumner, sedan rader i varje bit."""
    x0, y0, x1, y1 = box
    if x1 - x0 < 4 or y1 - y0 < 4 or depth > 3:
        return [box]
    H, W = text_blocks.shape
    sub = text_blocks[y0:y1, x0:x1] > 0
    out = []
    for axis in (0, 1):  # 0: dela på kolumner (andel text per x), 1: på rader
        prof = sub.mean(axis=axis)
        textish = prof > share
        if not textish.any():
            continue
        segs, start = [], None
        for i, t in enumerate(np.append(textish, True)):
            if not t and start is None:
                start = i
            elif t and start is not None:
                segs.append((start, i))
                start = None
        min_len = min_frac * (W if axis == 0 else H)
        segs = [s for s in segs if s[1] - s[0] >= min_len]
        for a, b in segs:
            piece = [x0 + a, y0, x0 + b, y1] if axis == 0 else [x0, y0 + a, x1, y0 + b]
            out.extend(_split_on_text(piece, text_blocks, min_frac, share, depth + 1))
        return out
    return [box]


def find_figures(norm: np.ndarray, words: list[dict]) -> list[list[int]]:
    """Illustrationer på en inskannad sida -> rutor [x0, y0, x1, y1] (i den
    förbehandlade bildens koordinater).

    Text maskas bort (OCR-ordrutor), resten smetas ihop till regioner. En region
    räknas som figur om den är stor nog OCH innehåller antingen en stor
    sammanhängande streckkomponent (konturer i en teckning) eller textur
    (raster/skraffering). Det skiljer bilder från handskrift, som består av
    många små, glesa streck i radband."""
    if not HAVE_CV:
        return []
    h, w = norm.shape
    den = cv2.medianBlur(norm, 3)
    ink = cv2.adaptiveThreshold(den, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
                                max(15, (min(h, w) // 60) | 1), 18)
    # Bara ord ur riktiga stycken räknas som text: Tesseract "läser" skräpord inne
    # i teckningar ("Y=", "Qs", "|") med konfidens > 60 (WEG40122 s.61: 33 st),
    # och maskas de blir serierutan en "textspalt". Stycke = Tesseracts (block, par).
    groups: dict = {}
    for wd in words:
        groups.setdefault((wd.get("b"), wd.get("p")), []).append(wd)
    real = set()
    for ws in groups.values():
        solid = sum(1 for x in ws if x["c"] >= 75 and sum(c.isalpha() for c in x["t"]) >= 2)
        if len(ws) >= 3 and solid / len(ws) >= 0.5:
            real.update(id(x) for x in ws)
    word_mask = np.zeros((h, w), np.uint8)
    for wd in words:
        if id(wd) not in real or wd["c"] < 60:
            continue
        pad = int(0.3 * wd["h"])
        word_mask[max(0, wd["y"] - pad):wd["y"] + wd["h"] + pad,
                  max(0, wd["x"] - pad):wd["x"] + wd.get("w", wd["h"]) + pad] = 255
    # Textspalter som solida block (ord + radmellanrum): bakgrundskonst bakom en
    # spalt binder annars ihop hela sidan till en "figur" (WEG40122 s.61).
    kb = max(5, int(0.025 * min(h, w)))
    text_blocks = cv2.morphologyEx(word_mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kb, kb)))
    ink[text_blocks > 0] = 0

    n, lab, st, _ = cv2.connectedComponentsWithStats(ink, 8)
    cw, ch, ca = st[:, cv2.CC_STAT_WIDTH], st[:, cv2.CC_STAT_HEIGHT], st[:, cv2.CC_STAT_AREA]
    thin = (cw > 20 * ch) | (ch > 20 * cw)  # linjal/spaltlinje/ram ensam är ingen bild
    fill = ca / np.maximum(1, cw * ch)
    # stor OCH tät: konturer i en teckning. Formulärramar/tabellrutnät är stora
    # men glesa (krigsdagboken 1915) och får inte göra sidan till en "figur".
    big = ((ch > 0.05 * h) | (cw > 0.15 * w)) & ~thin & (ca > 0.0005 * h * w) & (fill > 0.04)
    big[0] = False

    small = cv2.resize(ink, (max(1, w // 4), max(1, h // 4)), interpolation=cv2.INTER_AREA)
    kk = max(5, int(0.04 * min(h, w) / 4))
    dens = cv2.blur(small, (kk, kk))
    texture = cv2.resize((dens > int(TEXTURE_DENSITY * 255)).astype(np.uint8), (w, h),
                         interpolation=cv2.INTER_NEAREST)

    k = max(5, int(0.012 * min(h, w)))
    merged = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    merged[text_blocks > 0] = 0  # utsmetningen får inte bygga broar över textspalter
    n2, lab2, st2, _ = cv2.connectedComponentsWithStats(merged, 8)
    cands = []
    for j in range(1, n2):
        x, y, bw, bh = st2[j, 0], st2[j, 1], st2[j, 2], st2[j, 3]
        if bw < 0.08 * w or bh < 0.06 * h or bw * bh < 0.02 * w * h:
            continue
        for piece in _split_on_text([x, y, x + bw, y + bh], text_blocks):
            cands.append(_trim_text_edges(piece, text_blocks))
    boxes = []
    for x0, y0, x1, y1 in cands:
        x, y, bw, bh = x0, y0, x1 - x0, y1 - y0
        if bw < 0.08 * w or bh < 0.06 * h or bw * bh < 0.02 * w * h:
            continue
        region = (slice(y, y + bh), slice(x, x + bw))
        density = (ink[region] > 0).mean()
        if density < 0.03:
            continue
        if (text_blocks[region] > 0).mean() > 0.35:  # mest text = textspalt med rester
            continue
        # Andel av regionens bläck som ligger i stora, täta komponenter. Handskrift
        # = många små streck (låg andel), teckning = sammanhängande konturer.
        labs = lab[region][ink[region] > 0]
        big_share = big[labs].mean() if labs.size else 0.0
        has_texture = texture[region].mean() > 0.15
        if not (big_share >= 0.3 or has_texture):
            continue
        boxes.append([int(x), int(y), int(x + bw), int(y + bh)])

    # slå ihop överlappande/närliggande rutor – men bara om resultatet blir
    # kompakt. Två bilder i var sitt hörn (serieruta nere till vänster, konst uppe
    # till höger) har överlappande rektanglar men är två bilder, inte en helsida.
    gap = int(0.01 * max(h, w))

    def area(b):
        return max(0, b[2] - b[0]) * max(0, b[3] - b[1])

    changed = True
    while changed:
        changed = False
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                A, B = boxes[a], boxes[b]
                U = [min(A[0], B[0]), min(A[1], B[1]), max(A[2], B[2]), max(A[3], B[3])]
                touching = A[0] - gap <= B[2] and B[0] - gap <= A[2] and A[1] - gap <= B[3] and B[1] - gap <= A[3]
                if touching and area(U) <= 1.25 * (area(A) + area(B)):
                    boxes[a] = U
                    boxes.pop(b)
                    changed = True
                    break
            if changed:
                break
    pad = int(0.004 * max(h, w))
    return [[max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)]
            for x0, y0, x1, y1 in sorted(boxes, key=lambda b: (b[1], b[0]))]


def prepare(gray: np.ndarray) -> tuple[np.ndarray, dict]:
    """Rotation + uträtning (gemensamt för alla OCR-varianter)."""
    info = {"rotated": 0, "skew": 0.0}
    rot = osd_rotation(gray)
    if rot:
        gray = rotate90(gray, rot)
        info["rotated"] = rot
    sk = estimate_skew(gray)
    if sk:
        gray = rotate_small(gray, sk)
        info["skew"] = sk
    return gray, info
