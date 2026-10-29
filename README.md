# bugsafe-photos

BugSafe uygulamasının tür fotoğraflarını hazırlayan iş. Uygulama kodu burada **yok**; yalnız bu iş açık, çünkü
herkese açık repolarda GitHub Actions dakikaları ücretsiz.

- `build_photos.py` — GBIF (iNaturalist araştırma kalitesi gözlemleri) üzerinden tür başına en fazla 3 fotoğraf seçer.
  **Yalnız CC0 ve CC BY 4.0** lisanslı fotoğraflar (lisans fotoğraf başına kontrol edilir), 256/800 px WebP'ye çevirip
  Cloudflare R2'ye yükler. Kaldığı yerden devam eder.
- `photo_species.tsv` — tür listesi (GBIF Backbone Taxonomy anahtarları ve bilimsel adlar).
- `exclude.tsv` — elle çıkarılan fotoğraflar (`photo_id`).
- `.github/workflows/photos.yml` — Actions → **Species photos** → Run workflow (`limit` 0 = tüm türler).

- `build_sounds.py` — tür sesleri: `sound_species.tsv` listesindeki **CC0 / CC BY 4.0** iNaturalist seslerini indirir,
  ffmpeg ile en çok 20 sn'lik, ses düzeyi eşitlenmiş AAC m4a'ya çevirip R2'ye (`snd/`) yükler. Kaldığı yerden devam eder.
  Actions → **Species sounds** → Run workflow (`limit` 0 = tüm türler). `sound_exclude.tsv` — elle çıkarılan sesler.

Gerekli repo secret'ları: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.

Fotoğraflar ve sesler iNaturalist kullanıcılarına aittir; her biri uygulamada fotoğrafçının adıyla ve lisansıyla gösterilir.
Tür verisi: GBIF.org. Kaynak kodun asıl yeri özel repodaki `tools/photos/`; değişiklikler oradan buraya kopyalanır.
