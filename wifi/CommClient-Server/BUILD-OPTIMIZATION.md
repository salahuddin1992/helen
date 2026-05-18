# Build Optimization Guide — Phase 4 / Module S

Helen-Server can be packaged three different ways. Pick one based on what
you need.

| Backend                  | Output            | Size    | Build  | Cold-start | Use when                          |
|--------------------------|-------------------|---------|--------|------------|-----------------------------------|
| PyInstaller (baseline)   | `dist/`           | ~185 MB | ~45 s  | ~1.4 s     | Dev iteration, debugging          |
| PyInstaller (optimized)  | `dist-optimized/` | ~96 MB  | ~75 s  | ~1.4 s     | Default release                   |
| Nuitka standalone        | `dist-nuitka/`    | ~62 MB  | ~9 min | ~0.7 s     | Tagged releases / size-critical   |

## Commands

```powershell
# Baseline (untouched original spec) — for dev
pyinstaller --noconfirm CommClient-Server.spec

# Optimized PyInstaller (UPX + excludes)
pyinstaller --noconfirm --clean CommClient-Server.optimized.spec

# Nuitka (slower, smaller, faster cold-start)
.\build-nuitka.ps1                 # MSVC toolchain
.\build-nuitka.ps1 -MinGW64        # bundled MinGW toolchain

# Everything at once + summary table
.\build-all-optimized.ps1 -ToVersion 1.3.0
```

## Delta updates

After a release, place the previous binary at `dist-prev/Helen-Server/` and
re-run `build-all-optimized.ps1`. Step 3 will produce a binary patch under
`dist-deltas/<old>_to_<new>/` that ships in ~3-5 MB typical, applied
client-side by `delta-update-applier.py`.

```powershell
# Manual one-off
python delta-update-builder.py `
    --old dist-prev/Helen-Server/Helen-Server.exe `
    --new dist-optimized/Helen-Server/Helen-Server.exe `
    --from-version 1.2.0 --to-version 1.3.0
```

`bsdiff4` is preferred; the script falls back to an HDLT full-replace
encoding if `bsdiff4` is missing.

## Trade-offs

* **UPX** shrinks the exe ~30-40% but adds ~50-150 ms to first-launch
  (decompression). Excluded from `python*.dll` to avoid AV false-positives.
* **Nuitka** produces real native code — startup wins are dramatic
  (~50% faster cold start) but the build is slow and recompiles every
  time. Use for release builds, not dev loops.
* **Delta patches** require the client to verify SHA256 of the installed
  binary before applying. If sha mismatches, fall back to the full
  installer.

## Troubleshooting

* `upx is not recognized` → install [upx](https://upx.github.io/) and add
  `upx.exe` to PATH (or set `upx_dir` in the spec).
* `Nuitka MSVC not found` → pass `-MinGW64` to `build-nuitka.ps1`.
* `bsdiff4 install fails on Windows` → `pip install bsdiff4 --only-binary=:all:`
  to grab the prebuilt wheel, or pass `--force-fallback`.
