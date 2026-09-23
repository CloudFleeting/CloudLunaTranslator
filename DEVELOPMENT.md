# Windows development setup

LunaTranslator is a mixed Python and native Windows application. A source checkout does not contain the embedded Python runtime, native DLLs, hook binaries, OCR models, or launchers required to start the application.

## Requirements

- Windows 10 or later for the modern x64 build
- Git
- Python 3.13 matching the modern CI build
- Visual Studio 2026 with Desktop development with C++ and CMake components
- A Windows SDK supported by the selected build configuration
- NanaZip with a `K7C.exe`, `K7.exe`, or `NanaZipC.exe` execution alias, or Windows PowerShell ZIP support
- Node.js and npm for documentation builds

The full legacy release matrix additionally requires the Python, compiler, and SDK combinations declared in `.github/workflows/buildluna.yml`.

## Populate the development runtime

From the repository root, run:

```powershell
& .\src\scripts\setup_development.ps1
```

The script:

1. Reads `src/version.txt`.
2. Retrieves metadata for the exact matching upstream GitHub release.
3. Downloads `LunaTranslator_x64.zip` to a temporary directory.
4. Verifies the archive against the SHA-256 digest published by GitHub.
5. Extracts it with NanaZip when an execution alias is available.
6. Copies only generated launchers, runtimes, native components, models, and locale-helper binaries into `src`.
7. Creates an isolated `src/userconfig-dev` profile.
8. Removes the temporary archive unless `-KeepArchive` is specified.

The script refuses to bootstrap a release tag that does not match the checkout version.

## Run from source

From `src`, launch the normal executable with the isolated profile:

```powershell
.\LunaTranslator.exe --userconfig userconfig-dev
```

Use `LunaTranslator_admin.exe` only when a specific hook target requires elevation.

Before and after a runtime test, inspect the working tree:

```powershell
git status --short
```

Generated runtime files and the development profile are ignored. Application startup may still expose accidental changes to tracked configuration or localization resources, so a clean status remains an important validation step.

## Source validation

Python files can be syntax-checked with Python 3.13 without importing Windows DLLs by parsing them with `ast`. A real startup smoke test is still required because the Python/native boundary is initialized early in application startup.

Documentation is validated with:

```powershell
Set-Location docs
npm ci
npm run docs:build
```

The generated VitePress site is written beneath `docs/.vitepress/dist` and is ignored by Git.

## Native and release builds

The authoritative build matrix is in `.github/workflows/buildluna.yml`. Native components use CMake and Visual Studio generators, while packaging downloads several external runtime dependencies.

Do not treat a successful Python syntax check or GUI startup as native-hook validation. Changes to native utilities, subprocesses, or hooks require the relevant x86 and x64 builds and focused runtime testing. Public releases additionally require signing credentials and fork-owned release/update infrastructure.
