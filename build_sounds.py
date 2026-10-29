"""Tür sesleri: sound_species.tsv (beyinden; tür başına ≤3 CC0/CC-BY aday) → indir → ffmpeg → R2.

Adaylar özel repodaki brain/pipeline/sound_species.py ile seçilir (lisans ses başına kontrol edildi; atıf bilgisi
orada kalır, bu listede kişi adı yok). Burada yalnız dosya işlenir:
    baştaki sessizlik atılır → en çok MAX_SEC sn → ses düzeyi eşitlenir (loudnorm) → sonda kısma
    → AAC m4a, mono, 64 kbps, 44,1 kHz, faststart (akışla çalınır)
MIN_SEC'ten kısa, okunamayan ya da neredeyse sessiz dosya atılır; tür başına en çok PER_SPECIES ses kalır.
Çıktı (R2):
    snd/<sound_id>.m4a
    snd-index/sounds.tsv     sound_id, species, ord, seconds, bytes   (beyin bunu okur)
KALDIĞI YERDEN DEVAM: R2'de zaten olan snd/<id>.m4a yeniden işlenmez (süresi eski dizinden alınır).

    python build_sounds.py --species sound_species.tsv [--limit 20] [--upload] [--out out]
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
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

UA = "BugSafe-sounds/1.0 (+https://github.com/BuraakAI/bugsafe-photos)"
PER_SPECIES = 2
MAX_SEC = 20.0
MIN_SEC = 2.0
MAX_BYTES = 60 * 1024 * 1024         # daha büyük ham dosyayı indirme (uzun wav)
SILENT_DB = -55.0                    # ortalama düzey bunun altındaysa "sessiz" sayılır
DOWNLOAD_INTERVAL = 0.5              # sn — iNaturalist'e saniyede en çok 2 istek
EXCLUDE_FILE = os.path.join(os.path.dirname(__file__), "sound_exclude.tsv")


class Gate:
    def __init__(self, interval):
        self.lock, self.next_at, self.interval = threading.Lock(), 0.0, interval

    def wait(self):
        with self.lock:
            d = self.next_at - time.monotonic()
            if d > 0:
                time.sleep(d)
            self.next_at = time.monotonic() + self.interval


gate = Gate(DOWNLOAD_INTERVAL)


def download(url, tries=6):
    for i in range(tries):
        gate.wait()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                if int(r.headers.get("Content-Length") or 0) > MAX_BYTES:
                    raise ValueError("çok büyük")
                body = r.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("çok büyük")
                return body
        except urllib.error.HTTPError as e:
            if i == tries - 1 or e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(float(e.headers.get("Retry-After") or 0) or min(60, 2 ** i) + random.uniform(0, 1))
        except ValueError:
            raise
        except Exception:  # noqa: BLE001 — ağ hatası
            if i == tries - 1:
                raise
            time.sleep(min(60, 2 ** i) + random.uniform(0, 1))


def probe_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=60).stdout
    return float(json.loads(out)["format"]["duration"])


def mean_db(path):
    err = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=90).stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    return float(m.group(1)) if m else -99.0


def transcode(raw):
    """Ham ses → (m4a baytları, süre). Uygun değilse None."""
    with tempfile.TemporaryDirectory() as d:
        src, mid, dst = os.path.join(d, "src"), os.path.join(d, "mid.wav"), os.path.join(d, "out.m4a")
        open(src, "wb").write(raw)
        # 1) sessizlik + kırpma + düzey (loudnorm iç örneklemeyi yükseltir → 44,1 kHz'e geri)
        af = (f"silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.2,"
              f"atrim=duration={MAX_SEC},loudnorm=I=-18:TP=-1.5:LRA=11")
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn", "-ac", "1",
                            "-af", af, "-ar", "44100", mid], capture_output=True, stdin=subprocess.DEVNULL, timeout=90)
        if r.returncode or not os.path.exists(mid):
            return None
        sec = probe_seconds(mid)
        if sec < MIN_SEC or mean_db(src) < SILENT_DB:
            return None
        # 2) sonda 0,5 sn kısma → AAC
        fade = min(0.5, sec / 4)
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", mid,
                            "-af", f"afade=t=out:st={sec - fade:.3f}:d={fade:.3f}",
                            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", dst], capture_output=True, stdin=subprocess.DEVNULL, timeout=90)
        if r.returncode:
            return None
        return open(dst, "rb").read(), round(probe_seconds(dst), 1)


class Store:
    """--upload ise R2 (S3 API), değilse yerel klasör."""

    def __init__(self, upload, out):
        self.out, self.s3 = out, None
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
        if self.s3:
            try:
                return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except self.s3.exceptions.NoSuchKey:
                return None
        p = os.path.join(self.out, key)
        return open(p, "rb").read() if os.path.exists(p) else None

    def list(self, prefix):
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
        return [os.path.relpath(os.path.join(d, f), self.out) for d, _, fs in os.walk(root) for f in fs]

    def put(self, key, body, ctype, immutable=True):
        if self.s3:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=ctype,
                               CacheControl="public, max-age=31536000, immutable" if immutable else "public, max-age=300")
        else:
            p = os.path.join(self.out, key)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--species", required=True)
    ap.add_argument("--limit", type=int, default=0, help="tür sayısı (0 = hepsi)")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--out", default="out")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    exclude = set()
    if os.path.exists(EXCLUDE_FILE):
        exclude = {l.split("\t")[0].strip() for l in open(EXCLUDE_FILE) if l[:1] in "ig"}
    by_sp = {}
    for r in csv.reader(open(a.species), delimiter="\t"):
        if r and r[0].isdigit() and r[1] not in exclude:
            by_sp.setdefault(int(r[0]), []).append((r[1], r[2]))
    species = list(by_sp)
    if a.limit:
        species = species[:a.limit]

    store = Store(a.upload, a.out)
    have = {m.group(1) for k in store.list("snd/") if (m := re.match(r"snd/([ig][\d_]+)\.m4a$", k))}
    old = {}
    prev = store.get("snd-index/sounds.tsv")
    if prev:
        for r in csv.reader(io.StringIO(prev.decode()), delimiter="\t"):
            if r and r[0][:1] in "ig" and r[0] in have:
                old[r[0]] = (float(r[3]), int(r[4]))
    print(f"tür {len(species):,} · aday {sum(len(by_sp[s]) for s in species):,} · R2'de hazır ses {len(have):,}", flush=True)

    stats = {"ok": 0, "reused": 0, "bad": 0, "err": 0}
    lock = threading.Lock()

    def work(sp):
        rows = []
        for sid, url in by_sp[sp]:
            if len(rows) == PER_SPECIES:
                break
            if sid in old:
                rows.append((sid, *old[sid]))
                with lock:
                    stats["reused"] += 1
                continue
            try:
                res = transcode(download(url))
            except Exception as e:  # noqa: BLE001
                with lock:
                    stats["err"] += 1
                print(f"  {sp} {sid}: {type(e).__name__} {str(e)[:80]}", flush=True)
                continue
            if not res:
                with lock:
                    stats["bad"] += 1
                continue
            body, sec = res
            store.put(f"snd/{sid}.m4a", body, "audio/mp4")
            if stats["ok"] < 20:
                print(f"  {sp} {sid}: {sec} sn · {len(body) // 1024} KB", flush=True)
            rows.append((sid, sec, len(body)))
            with lock:
                stats["ok"] += 1
        return sp, rows

    index, t0 = [], time.monotonic()
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for i, (sp, rows) in enumerate(ex.map(work, species), 1):
            index += [(sid, sp, ord_, sec, size) for ord_, (sid, sec, size) in enumerate(rows)]
            if i % 50 == 0:
                print(f"{i:,}/{len(species):,} tür · {stats} · {time.monotonic() - t0:.0f} sn", flush=True)

    index.sort(key=lambda r: (r[1], r[2]))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(["sound_id", "species", "ord", "seconds", "bytes"])
    w.writerows(index)
    store.put("snd-index/sounds.tsv", buf.getvalue().encode(), "text/tab-separated-values", immutable=False)
    os.makedirs(a.out, exist_ok=True)
    open(os.path.join(a.out, "sounds.tsv"), "w").write(buf.getvalue())
    n_sp = len({r[1] for r in index})
    mb = sum(r[4] for r in index) / 1e6
    print(f"bitti: sesli tür {n_sp:,} · ses {len(index):,} · {mb:.1f} MB · {stats}", flush=True)


if __name__ == "__main__":
    main()
