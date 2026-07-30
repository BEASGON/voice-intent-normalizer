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
root, hotword, and payload directory handles that request only directory-read
access while denying delete sharing for the whole protected operation.
Ordinary loader and updater guards can therefore coexist, while replacement
of a supported direct state directory remains denied until every guard closes.

On POSIX, V1 requires an absolute normalized path and rejects a symbolic link
in any existing supplied component. The implementation-defined `//` spelling
is also rejected. Portable Python does not provide a reliable cross-platform
way to classify every network filesystem or bind mount, so V1 does not guess:
operators must configure the direct path on a local filesystem.

Once a root is guarded, state files are opened relative to the retained
directory identity. POSIX uses standard `dir_fd` operations for bounded reads,
no-follow opens, one-component directory creation, metadata checks, unlink,
and atomic temp-file/fsync/replace writes. It does not depend on
`/proc/self/fd`, `/dev/fd`, or another descriptor filesystem. A platform that
cannot provide the required standard descriptor-relative operations fails
closed with a platform capability error.

Requested optional directories are either retained or explicitly unavailable
for that lease. A directory that was absent when a loader acquired its guard
cannot become available through a later symbolic-link or junction injection.
With creation enabled, each component is created relative to its retained
parent, opened without following aliases, and retained before the operation
continues.

An invalid root is rejected before hotword network access, journal recovery,
or state mutation. `update_hotwords` returns `REJECTED` with:

```text
state root rejected: direct canonical local path required
```

`StatePaths.resolve` and `LexiconSet.load` raise the corresponding validation
error. The low-level hotword resolver fails closed and returns no hotword
snapshot. A successful resolution returns the validated bytes read while the
same lease binds `current.json` and its checksum-addressed payload; it never
returns a path to be reopened after the guard has been released.
