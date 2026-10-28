"""Tür fotoğrafları: GBIF (iNaturalist araştırma kalitesi) → tür başına ≤3 CC0/CC-BY fotoğraf → WebP → R2.

Kaynak: GBIF occurrence API, iNaturalist veri seti; tür anahtarı beyindekiyle aynı (eş ad sorunu yok).
Lisans FOTOĞRAF başına kontrol edilir (kayıt CC-BY olsa da fotoğraf CC-BY-NC/SA olabilir) — yalnız CC0 ve CC-BY 4.0.
Bir gözlemden en fazla bir fotoğraf. Tür başına en fazla 8 aday küçük boyutta indirilir, basit bir kalite puanıyla
(netlik × konunun ortada/büyük olması) en iyi 3'ü seçilir. Kötü bir fotoğraf tools/photos/exclude.tsv'ye
(photo_id) yazılarak bir sonraki çalıştırmada dışarıda bırakılır. Çıktı (R2):
    sp/<tür>/<photo_id>_256.webp   kare kırpma, listeler
    sp/<tür>/<photo_id>_800.webp   uzun kenar 800, detay
(Yol fotoğraf numarasını taşır: yeniden çalıştırmada başka fotoğraf seçilse bile eski adres eski fotoğrafı gösterir →
uygulamadaki atıf her zaman doğru fotoğrafa aittir.)
    photo-index/progress/s<i>of<n>.tsv    shard'ın ilerlemesi (≈100 türde bir yazılır; iş kesilse de kaybolmaz)
    photo-index/progress/s<i>of<n>.none   denenip uygun fotoğrafı OLMAYAN türler (bir daha sorulmaz)
Dizin satırı: species, ord, photo_id, license, creator, color(0xRRGGBB), w, h, obs_key

KALDIĞI YERDEN DEVAM: başlarken tüm progress dosyaları okunur → bitmiş türler atlanır. R2'de zaten görüntüsü olan
türlerde (önceki yarım çalıştırma) yalnız GBIF'ten künye (lisans, fotoğrafçı) alınır, görüntü yeniden indirilmez.
GBIF'e nazik: shard başına tek istek, istekler arası ≥ GBIF_INTERVAL sn. --deadline-min dolunca yeni tür başlatmaz,
ilerlemeyi yazar ve temiz çıkar (GitHub süre sınırından önce).

    python build_photos.py --species seeds.tsv --shard 0 --shards 4 [--limit 20] [--deadline-min 330] [--upload] [--out dir]
Ortam (yalnız --upload): R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
"""
import argparse
import concurrent.futures as cf
import csv
import io
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageFilter, ImageOps, ImageStat

INAT_DATASET = "50c9509d-22c7-4a22-a47d-8c48425ef4a7"
GBIF = "https://api.gbif.org/v1/occurrence/search"
UA = "BugSafe-photos/1.0 (+https://github.com/BuraakAI/bugsafe-photos)"
PER_SPECIES = 3
CANDIDATES = 40                      # tür başına bakılan kayıt (lisanslı fotoğraf bulmak için)
SCORED = 8                           # puanlanan aday (küçük "medium" boyutta indirilir)
MIN_RELATIVE = 0.35                  # 2./3. fotoğraf en iyinin puanının en az bu oranı olmalı
EXCLUDE_FILE = os.path.join(os.path.dirname(__file__), "exclude.tsv")
OK_LICENSES = {                      # fotoğraf lisansı URL'si → kısa ad
    "creativecommons.org/publicdomain/zero/1.0": "cc0",
    "creativecommons.org/licenses/by/4.0": "cc-by",
}
PHOTO_ID = re.compile(r"/photos/(\d+)/")
GBIF_INTERVAL = 1.1                  # sn — 8 shard × 2 eşzamanlıda binlerce 429 aldık (2026-10-27)
FLUSH_EVERY = 100
HEADER = ["species", "ord", "photo_id", "license", "creator", "color", "w", "h", "obs_key"]


class GbifGate:
    """Shard başına tek GBIF isteği (yanıt gelene kadar sıradaki bekler) ve istekler arası en az GBIF_INTERVAL sn.
    Örtüşen istekler denendi: aralıklı 429 artıyor (2026-10-28) → tek istek kalıyor."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next_at = 0.0

    def __enter__(self):
        self.lock.acquire()
        wait = self.next_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def __exit__(self, *exc):
        self.next_at = time.monotonic() + GBIF_INTERVAL
        self.lock.release()


gbif_gate = GbifGate()


def http(url, tries=8):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if i == tries - 1 or e.code not in (429, 500, 502, 503, 504):
                raise
            # 429: sunucunun istediği kadar bekle (Retry-After), yoksa üstel geri çekilme + sarsıntı
            wait = float(e.headers.get("Retry-After") or 0) or min(60, 2 ** i)
            time.sleep(wait + random.uniform(0, 1.5))
        except Exception:  # noqa: BLE001 — ağ hatası: geri çekil, tekrar dene
            if i == tries - 1:
                raise
            time.sleep(min(60, 2 ** i) + random.uniform(0, 1.5))


def license_of(url):
    u = (url or "").lower().replace("https://", "").replace("http://", "").rstrip("/")
    u = u.removesuffix("/legalcode").removesuffix("/deed.en")
    return OK_LICENSES.get(u)


def candidates(species_key):
    q = urllib.parse.urlencode([("taxonKey", species_key), ("datasetKey", INAT_DATASET),
                                ("mediaType", "StillImage"), ("license", "CC0_1_0"), ("license", "CC_BY_4_0"),
                                ("limit", CANDIDATES)])
    with gbif_gate:
        data = json.loads(http(f"{GBIF}?{q}"))
    out = []
    for rec in data.get("results", []):
        if rec.get("taxonKey") != species_key and rec.get("speciesKey") != species_key:
            continue
        for m in rec.get("media", []):
            lic = license_of(m.get("license"))
            pid = PHOTO_ID.search(m.get("identifier") or "")
            if lic and pid and m.get("type") == "StillImage":
                out.append({"photo_id": int(pid.group(1)), "url": m["identifier"], "license": lic,
                            "creator": (m.get("rightsHolder") or m.get("creator") or "").strip(), "obs": rec["key"]})
                break   # gözlem başına bir fotoğraf
    return out


def load_exclude():
    if not os.path.exists(EXCLUDE_FILE):
        return set()
    return {int(l.split("\t")[0]) for l in open(EXCLUDE_FILE) if l.strip() and l[0].isdigit()}


def quality(raw):
    """Kaba kalite puanı: kenar (netlik) yoğunluğu ortadaki %50'lik alanda yüksek, kenarlarda düşük olsun.
    Ağaç kovuğunda nokta kadar arı ya da bulanık fotoğraf düşük puan alır."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("L")
    im.thumbnail((320, 320))
    edges = im.filter(ImageFilter.FIND_EDGES)
    w, h = edges.size
    center = edges.crop((w // 4, h // 4, w * 3 // 4, h * 3 // 4))
    c, a = ImageStat.Stat(center).mean[0], ImageStat.Stat(edges).mean[0]
    # Oran üçüncü kuvvetle: her yeri aynı dokulu zemin (kumaş, çakıl) oran ~1 → yüksek netliğe rağmen düşük puan
    return c * (c / (a + 1e-6)) ** 3


def color_and_size(webp800):
    img = Image.open(io.BytesIO(webp800)).convert("RGB")
    r, g, b = img.resize((1, 1), Image.BOX).getpixel((0, 0))
    return (r << 16) | (g << 8) | b, img.size


def variants(raw):
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")
    detail = img.copy()
    detail.thumbnail((800, 800), Image.LANCZOS)
    thumb = ImageOps.fit(img, (256, 256), Image.LANCZOS)
    r, g, b = img.resize((1, 1), Image.BOX).getpixel((0, 0))

    def webp(im, q):
        b = io.BytesIO()
        im.save(b, "WEBP", quality=q, method=4)   # 6 en küçük dosyayı verir ama 2 çekirdekli makinede çok yavaş
        return b.getvalue()

    return webp(thumb, 72), webp(detail, 78), (r << 16) | (g << 8) | b, detail.size


class Store:
    """--upload ise R2 (S3 API), değilse yerel klasör."""

    def __init__(self, upload, out):
        self.out = out
        self.s3 = None
        if upload:
            import boto3
            from botocore.config import Config
            self.bucket = os.environ["R2_BUCKET"]
            self.s3 = boto3.client(
                "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"], aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto", config=Config(retries={"max_attempts": 8, "mode": "adaptive"},
                                                  request_checksum_calculation="when_required",
                                                  response_checksum_validation="when_required"))

    def get(self, key):
        """Nesneyi döndürür; yoksa None."""
        if self.s3:
            try:
                return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except self.s3.exceptions.NoSuchKey:
                return None
        p = os.path.join(self.out, key)
        return open(p, "rb").read() if os.path.exists(p) else None

    def list(self, prefix):
        """prefix altındaki tüm anahtarlar."""
        if self.s3:
            keys, token = [], None
            while True:
                kw = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
                if token:
                    kw["ContinuationToken"] = token
                r = self.s3.list_objects_v2(**kw)
                keys += [o["Key"] for o in r.get("Contents", [])]
                if not r.get("IsTruncated"):
                    return keys
                token = r["NextContinuationToken"]
        root = os.path.join(self.out, prefix)
        out = []
        for d, _, files in os.walk(root):
            out += [os.path.relpath(os.path.join(d, f), self.out) for f in files]
        return out

    def put(self, key, body, ctype, immutable=True):
        if self.s3:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=ctype,
                               CacheControl="public, max-age=31536000, immutable" if immutable else "public, max-age=300")
        else:
            p = os.path.join(self.out, key)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(body)


def existing_photos(store):
    """R2'de iki boyutu da yüklü fotoğraflar: tür → {photo_id} (önceki yarım çalıştırmaların emeği)."""
    seen = {}
    for k in store.list("sp/"):
        m = re.match(r"sp/(\d+)/(\d+)_(256|800)\.webp$", k)
        if m:
            seen.setdefault((int(m.group(1)), int(m.group(2))), set()).add(m.group(3))
    out = {}
    for (sp, pid), sizes in seen.items():
        if sizes == {"256", "800"}:
            out.setdefault(sp, set()).add(pid)
    return out


def process(species_key, store, exclude, have=frozenset()):
    cands = [c for c in candidates(species_key) if c["photo_id"] not in exclude]
    reuse = [c for c in cands if c["photo_id"] in have][:PER_SPECIES]
    if reuse:   # görüntüler zaten R2'de: yalnız künye + renk/boyut (R2'den, indirme/dönüştürme yok)
        rows = []
        for c in reuse:
            body = store.get(f"sp/{species_key}/{c['photo_id']}_800.webp")
            if body is None:
                continue
            color, (w, h) = color_and_size(body)
            rows.append([species_key, len(rows), c["photo_id"], c["license"], c["creator"], color, w, h, c["obs"]])
        if rows:
            return rows
    scored = []
    for c in cands[:SCORED]:
        try:   # puanlama 240 px "small" boyutta (500 px'e göre ~4× az indirme + çözme)
            scored.append((quality(http(c["url"].replace("/original.", "/small."))), c))
        except Exception:  # noqa: BLE001 — indirilemeyen/bozuk aday atlanır
            continue
    scored.sort(key=lambda x: -x[0])
    best = scored[0][0] if scored else 0
    rows = []
    for score, c in scored:
        if len(rows) == PER_SPECIES:
            break
        if rows and score < MIN_RELATIVE * best:   # 2. ve 3. fotoğraf zayıfsa boş kalsın
            break
        try:
            raw = http(c["url"].replace("/original.", "/large."))
            thumb, detail, color, (w, h) = variants(raw)
        except Exception:  # noqa: BLE001 — bu fotoğrafı atla, sıradakine geç
            continue
        ord_ = len(rows)
        store.put(f"sp/{species_key}/{c['photo_id']}_256.webp", thumb, "image/webp")
        store.put(f"sp/{species_key}/{c['photo_id']}_800.webp", detail, "image/webp")
        rows.append([species_key, ord_, c["photo_id"], c["license"], c["creator"], color, w, h, c["obs"]])
    return rows


def read_progress(store):
    """Tüm progress dosyaları → (satırlar, bitmiş türler). Shard sayısı değişse de geçerli."""
    rows, done = [], set()
    for key in sorted(store.list("photo-index/progress/")):
        body = (store.get(key) or b"").decode()
        if key.endswith(".none"):
            done |= {int(x) for x in body.split() if x.isdigit()}
        elif key.endswith(".tsv"):
            for r in csv.reader(io.StringIO(body), delimiter="\t"):
                if r and r[0].isdigit():
                    rows.append(r)
                    done.add(int(r[0]))
    return rows, done


def write_progress(store, name, rows, none):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(HEADER)
    w.writerows(sorted(rows, key=lambda r: (int(r[0]), int(r[1]))))
    store.put(f"photo-index/progress/{name}.tsv", buf.getvalue().encode(), "text/tab-separated-values", immutable=False)
    store.put(f"photo-index/progress/{name}.none", "\n".join(map(str, sorted(none))).encode(), "text/plain", immutable=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--species", required=True)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="shard başına en fazla tür (0 = hepsi)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--deadline-min", type=float, default=0, help="bu kadar dakika sonra yeni tür başlatma (0 = sınırsız)")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--out", default="photos_out")
    a = ap.parse_args()

    keys = [int(line.split("\t")[0]) for line in open(a.species) if line.strip() and not line.startswith("#")]
    keys = keys[a.shard::a.shards]
    if a.limit:
        keys = keys[:a.limit]
    store = Store(a.upload, a.out)
    exclude = load_exclude()
    name = f"s{a.shard}of{a.shards}"
    t0 = time.time()

    _, done_all = read_progress(store)
    mine_rows = []
    body = store.get(f"photo-index/progress/{name}.tsv")   # kendi dosyamız: üstüne eklenir
    if body:
        mine_rows = [r for r in csv.reader(io.StringIO(body.decode()), delimiter="\t") if r and r[0].isdigit()]
    mine_none = set(int(x) for x in (store.get(f"photo-index/progress/{name}.none") or b"").decode().split() if x.isdigit())
    have = existing_photos(store)
    todo = [k for k in keys if k not in done_all]
    print(f"{name}: {len(keys):,} tür · bitmiş {len(keys) - len(todo):,} · kalan {len(todo):,} · "
          f"R2'de görüntüsü olan tür {len(have):,}", flush=True)

    deadline = t0 + a.deadline_min * 60 if a.deadline_min else None
    lock = threading.Lock()
    stats = {"done": 0, "photos": 0, "reused": 0, "none": 0, "failed": 0}

    def work(k):
        rows = process(k, store, exclude, frozenset(have.get(k, ())))
        with lock:
            if rows:
                mine_rows.extend([[str(x) for x in r] for r in rows])
                stats["photos"] += len(rows)
                stats["reused"] += sum(1 for r in rows if r[2] in have.get(k, ()))
            else:
                mine_none.add(k)
                stats["none"] += 1

    it = iter(todo)
    stopped = False
    with cf.ThreadPoolExecutor(a.workers) as ex:
        running = set()
        while True:
            while len(running) < a.workers * 2 and not stopped:
                if deadline and time.time() > deadline:
                    stopped = True
                    break
                k = next(it, None)
                if k is None:
                    break
                f = ex.submit(work, k)
                f.key = k
                running.add(f)
            if not running:
                break
            finished, running = cf.wait(running, return_when=cf.FIRST_COMPLETED)
            for f in finished:
                if f.exception():   # GBIF hâlâ reddediyorsa: bitmiş sayılmaz, bir sonraki çalıştırmada denenir
                    stats["failed"] += 1
                    print(f"  ! {f.key}: {f.exception()}", file=sys.stderr, flush=True)
                stats["done"] += 1
                if stats["done"] % FLUSH_EVERY == 0:
                    with lock:
                        write_progress(store, name, list(mine_rows), set(mine_none))
                    print(f"  {stats['done']:,}/{len(todo):,} · {time.time() - t0:.0f} sn · {stats}", flush=True)
    with lock:
        write_progress(store, name, list(mine_rows), set(mine_none))

    left = len(todo) - stats["done"] + stats["failed"]
    print(f"bitti: bu çalıştırmada {stats['done']:,} tür · fotoğraf {stats['photos']:,} (R2'den yeniden {stats['reused']:,}) · "
          f"uygun fotoğrafı yok {stats['none']:,} · başarısız {stats['failed']:,} · kalan {left:,} · {time.time() - t0:.0f} sn"
          + (" · SÜRE DOLDU, sonraki çalıştırma devam eder" if stopped else ""))


if __name__ == "__main__":
    main()
