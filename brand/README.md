# IRIS branding

Product: **IRIS**, repository `levtos/benni_scene_presets`, technical domain `benni_scene_presets`.
Part of the Unicorn Station family; binding assignment: https://github.com/levtos/control/issues/59.

## Assets and provenance

- `original/IRIS.png`: byte-identical approved 1254 × 1254 RGB PNG from `core_contracts_logos.zip`.
- `logos/logo-{1024,512,256,128}.png`: complete square logo, including unchanged wordmark.
- `icons/icon-{512,256,128,64,32}.png`: symbol-only square crop, source box `(177, 0, 1077, 900)`, then proportional LANCZOS downsampling.
- `assets.json`: source and export SHA-256, dimensions, crop and resampling provenance.

These are raster PNG assets, not vectors. The source has an opaque baked background and pale rounded corners; no transparency, color substitution, sharpening, symbol redraw or synthesized pixels were introduced. Icon-only files intentionally omit the wordmark; the complete original remains unchanged. Dark variants use identical pixels because the approved background already supplies contrast. At 32/64 px use the symbol, not a miniature wordmark.

## Home Assistant display

Home Assistant **2026.3+** reads `custom_components/benni_scene_presets/brand/` through `/api/brands/integration/benni_scene_presets/{image}`. Runtime files: `icon.png` 256, `icon@2x.png` 512, `logo.png` 256, `logo@2x.png` 512 and byte-identical `dark_` counterparts. HACS packages these inside the integration directory. Manifest and HACS names include the brand; technical domain and existing entry/entity/service/panel identities remain unchanged. Existing user-named config entries and panel labels are preserved.

Older HA versions keep their existing supported behavior but do not load these local assets. For their integration pictures, a separate PR to `home-assistant/brands` under `custom_integrations/benni_scene_presets/` is required; the eight runtime PNGs are the prepared submission assets. Some HACS versions also fetch update pictures directly from `brands.home-assistant.io`, so local HA branding does not guarantee a HACS update-card picture. No external Brands submission or HA installation was performed by this release.

Version 0.1.5 is a branding-only patch: no policy/engine behavior, dependencies, API paths, database, registry identities or active HA configuration changes. Installation/restart and real HA light/dark verification remain Benni's separate gate.

References: https://developers.home-assistant.io/docs/core/integration/brand_images/ and https://developers.home-assistant.io/docs/apps/presentation/.
