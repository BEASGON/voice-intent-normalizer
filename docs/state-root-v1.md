# V1 state-root path contract

V1 accepts only a direct, canonical, local state-root path. This restriction
keeps hotword update locking and authority checks unambiguous.

The path must be absolute and lexically normalized. `.` and `..` components
are rejected. The final directory may be absent, but every existing component
of the supplied path must be a direct directory rather than an alias.

On Windows, V1 accepts a local drive-letter path. Ordinary case and separator
differences and the `\\?\C:\...` representation are normalized. UNC shares,
Win32 device paths, mapped network drives, junctions, symbolic links, other
reparse points, short-name aliases, and redirected drive aliases are rejected.
The updater retains its canonical-path `Global\` mutex and holds no-follow
root, hotword, and payload directory handles that deny delete sharing for the
whole protected operation. Replacement of a supported direct state directory
is therefore serialized and its live directory chain cannot be renamed out
from under a Windows update.

On POSIX, V1 requires an absolute normalized path and rejects a symbolic link
in any existing supplied component. The implementation-defined `//` spelling
is also rejected. Portable Python does not provide a reliable cross-platform
way to classify every network filesystem or bind mount, so V1 does not guess:
operators must configure the direct path on a local filesystem.

An invalid root is rejected before hotword network access, journal recovery,
or state mutation. `update_hotwords` returns `REJECTED` with:

```text
state root rejected: direct canonical local path required
```

`StatePaths.resolve` and `LexiconSet.load` raise the corresponding validation
error. The low-level hotword resolver fails closed and returns no state file.
