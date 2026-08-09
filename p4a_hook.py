"""python-for-android build hook -- adds android:requestLegacyExternalStorage.

Why this exists
----------------
Our app reads back plain files that Termux writes under /sdcard/termux_wrapper/
(install/run logs, saved config). On Android 10 (API 29), apps targeting
API 29+ are scoped-storage-restricted and such plain-path reads fail with
PermissionError -- confirmed on a real Galaxy S9+ running Android 10.

Android 10 (and only Android 10/11) will restore full legacy filesystem
access to a scoped-storage app if its manifest carries
android:requestLegacyExternalStorage="true", regardless of targetSdkVersion.
Android 12+ ignores this flag entirely (no effect, also no harm).

buildozer.spec / python-for-android has no direct spec key for this: the
PR that would add `android.extra_manifest_application_arguments` is still
open/unmerged upstream (kivy/python-for-android#2691). The officially
supported workaround is p4a's own hook system (`p4a.hook = p4a_hook.py`),
which runs this script inside the build and lets it patch the generated
AndroidManifest.xml on disk before Gradle assembles the APK.
"""
from pathlib import Path

ATTRIBUTE = 'android:requestLegacyExternalStorage="true"'


def _patch(toolchain):
    manifest_path = Path(toolchain._dist.dist_dir) / "src" / "main" / "AndroidManifest.xml"
    if not manifest_path.is_file():
        # Older p4a bootstrap layout keeps it at the dist root instead.
        manifest_path = Path(toolchain._dist.dist_dir) / "AndroidManifest.xml"
    if not manifest_path.is_file():
        print("[p4a_hook] AndroidManifest.xml not found, skipping patch")
        return

    manifest = manifest_path.read_text(encoding="utf-8")
    if ATTRIBUTE in manifest:
        return

    patched = manifest.replace("<application ", f"<application {ATTRIBUTE} ", 1)
    if patched == manifest:
        print("[p4a_hook] <application> tag not found, skipping patch")
        return

    manifest_path.write_text(patched, encoding="utf-8")
    print(f"[p4a_hook] Added {ATTRIBUTE} to {manifest_path}")


def after_apk_build(toolchain):
    _patch(toolchain)


def before_apk_assemble(toolchain):
    _patch(toolchain)
