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
