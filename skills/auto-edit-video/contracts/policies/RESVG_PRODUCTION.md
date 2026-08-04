# resvg production adapter contract

- Production never discovers `resvg` through `PATH`. A user must run
  `scripts/configure_resvg.py --resvg <canonical-absolute-path>`.
- The default machine manifest is
  `~/.config/auto-edit-video/resvg-manifest.json`; an absolute
  `AUTO_EDIT_VIDEO_RESVG_MANIFEST` may override it. The manifest and its stable
  sibling `resvg-sandbox.sb` are atomically written as owner-only `0600` files.
- Loading is duplicate-free strict JSON with exact keys. The manifest, profile,
  resvg executable, `/usr/bin/sandbox-exec`, exact version output, and all
  SHA-256 values are revalidated fail-closed. The machine profile bytes must be
  byte-for-byte equal to the compiled reviewed profile; a manifest cannot
  self-sign a different profile.
- The reviewed sandbox is deny-by-default, explicitly denies all network
  access, permits process execution only for the pinned resvg path, and gives
  file access only to the private canonical work directory, the pinned binary,
  required macOS system paths, and minimal device files. It grants no home
  directory read scope.
- Both probe and render use fixed argv without a shell, a fixed environment,
  `--quiet`, `--skip-system-fonts`, and a private `--resources-dir`. The probe
  requires a sandboxed exact `--version` and validated 1x1 PNG smoke raster.
- Runtime enforcement rejects after 5 seconds, over 64 MiB of output, or over
  64 KiB on either stdout or stderr. The 64 MiB output bound is supervised at
  50 ms intervals and rechecked before publication; it is rejection plus
  process-group termination, not a kernel `RLIMIT_FSIZE`. PNG bytes still pass
  the strict PNG parser.
- No 256 MiB memory-cap claim is made. Python cannot safely apply a child-only
  `RLIMIT_AS` via `preexec_fn` from the threaded macOS Studio process. The
  adapter records `memory_limit_enforced: false` instead of weakening process
  safety or claiming a limit that is not enforced.
- The same-UID machine account is a trusted boundary between hash validation
  and path-based `sandbox-exec`/resvg execution. Owner, mode, symlink, exact
  bytes, and hashes fail closed across other-UID or accidental drift, but this
  adapter does not claim protection from a malicious same-UID process racing a
  configured executable or profile replacement between validation and exec.
- Canonical sanitized SVG is the only rasterizer input. Raw SVG, external/file/
  data references, DOCTYPE, style, scripts, and animation remain rejected
  before the process runner is reached; raw or sanitized SVG never enters the
  DOM or timeline.
