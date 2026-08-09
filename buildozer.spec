[app]

title = Script Wrapper
package.name = scriptwrapper
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

version = 1.0.0

# main.py imports these; pyjnius drives the Termux RUN_COMMAND intent,
# plyer provides the native Android file picker.
requirements = python3,kivy==2.3.0,pyjnius,plyer,android

orientation = portrait
fullscreen = 0

# Uncomment and provide a 512x512 PNG if you have one.
# icon.filename = %(source.dir)s/icon.png

# READ/WRITE_EXTERNAL_STORAGE: needed to read the selected .py file and
# write /sdcard/config.json + /sdcard/termux_wrapper/*.log.
# com.termux.permission.RUN_COMMAND: required by Termux to accept our
# RUN_COMMAND intent (Termux declares this as a signature/normal permission
# depending on version; requesting it here is required either way).
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,com.termux.permission.RUN_COMMAND

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
