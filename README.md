# bugsafe-photos

BugSafe uygulamasının tür fotoğraflarını hazırlayan iş. Uygulama kodu burada **yok**; yalnız bu iş açık, çünkü
herkese açık repolarda GitHub Actions dakikaları ücretsiz.

- `build_photos.py` — GBIF (iNaturalist araştırma kalitesi gözlemleri) üzerinden tür başına en fazla 3 fotoğraf seçer.
  **Yalnız CC0 ve CC BY 4.0** lisanslı fotoğraflar (lisans fotoğraf başına kontrol edilir), 256/800 px WebP'ye çevirip
  Cloudflare R2'ye yükler. Kaldığı yerden devam eder.
- `photo_species.tsv` — tür listesi (GBIF Backbone Taxonomy anahtarları ve bilimsel adlar).
- `exclude.tsv` — elle çıkarılan fotoğraflar (`photo_id`).
- `.github/workflows/photos.yml` — Actions → **Species photos** → Run workflow (`limit` 0 = tüm türler).

Gerekli repo secret'ları: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.

Fotoğraflar iNaturalist kullanıcılarına aittir; her fotoğraf uygulamada fotoğrafçının adıyla ve lisansıyla gösterilir.
Tür verisi: GBIF.org. Kaynak kodun asıl yeri özel repodaki `tools/photos/`; değişiklikler oradan buraya kopyalanır.
