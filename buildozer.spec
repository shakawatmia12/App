[app]

title = Script Wrapper
package.name = scriptwrapper
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

version = 1.0.0

# main.py imports these; pyjnius drives the Termux RUN_COMMAND intent and
# the storage-settings intent. plyer drives the file picker via Android's
# Storage Access Framework (SAF) -- SAF is exempt from the scoped-storage
# directory-listing restriction that broke Kivy's own raw FileChooser.
# NOTE: do not pin kivy's version here. python-for-android's bundled
# python3 recipe tracks a recent CPython (currently 3.14), and older Kivy
# releases (e.g. 2.3.0) ship pre-generated Cython C code that fails to
# compile against newer CPython C-API internals. Leaving kivy unpinned
# lets pip resolve the latest release, which carries the compatibility fix.
requirements = python3,kivy,pyjnius,plyer,android

orientation = portrait
fullscreen = 0

# Uncomment and provide a 512x512 PNG if you have one.
# icon.filename = %(source.dir)s/icon.png

# READ/WRITE_EXTERNAL_STORAGE: legacy storage permissions, still relevant
# on API < 30 devices.
# MANAGE_EXTERNAL_STORAGE: required on Android 11+ (API 30+) to open plain
# paths like /sdcard/config.json or a script picked from anywhere in
# shared storage -- normal WRITE_EXTERNAL_STORAGE is not enough there.
# It is a "special" permission the user grants via Settings (see
# request_storage_access() in main.py); listing it here just declares it.
# com.termux.permission.RUN_COMMAND: required by Termux to accept our
# RUN_COMMAND intent (Termux declares this as a signature/normal permission
# depending on version; requesting it here is required either way).
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,com.termux.permission.RUN_COMMAND

android.api = 33
android.minapi = 24
android.ndk = 25b
# Single arch keeps the first CI build faster/less failure-prone.
# Add ",armeabi-v7a" back once a build succeeds if you need 32-bit devices too.
android.archs = arm64-v8a
android.allow_backup = True
android.accept_sdk_license = True

# We use an *explicit* intent (setClassName) to reach Termux's
# RunCommandService, which is exempt from Android 11+ package-visibility
# filtering -- so no <queries> manifest entry is needed here.

[buildozer]

log_level = 2
warn_on_root = 1
