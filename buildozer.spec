[app]

title = Script Wrapper
package.name = scriptwrapper
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

version = 1.0.0

# main.py imports these; pyjnius drives the Termux RUN_COMMAND intent and
# the storage-settings intent. androidstorage4kivy is the primary file
# picker/reader: it hands back the SAF content reference (not a resolved
# path) and reads it via ContentResolver, which is the only thing that
# reliably bypasses Android 10+ scoped storage for our own process. plyer
# is kept as a secondary picker for platforms/older Android where that
# isn't needed.
# NOTE: do not pin kivy's version here. python-for-android's bundled
# python3 recipe tracks a recent CPython (currently 3.14), and older Kivy
# releases (e.g. 2.3.0) ship pre-generated Cython C code that fails to
# compile against newer CPython C-API internals. Leaving kivy unpinned
# lets pip resolve the latest release, which carries the compatibility fix.
requirements = python3,kivy,pyjnius,plyer,androidstorage4kivy,android

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
# WAKE_LOCK: needed for the PARTIAL_WAKE_LOCK held for the duration of a
# script run (see main.py's _acquire_wakelock) -- keeps the CPU from
# deep-sleeping mid-run if the screen times out, independent of whatever
# the OS/OEM battery manager decides about the process itself.
# REQUEST_IGNORE_BATTERY_OPTIMIZATIONS: needed to show the system's own
# "exempt this app from battery optimization" dialog (see
# request_battery_optimization_exemption) -- this is the actual, real
# mitigation for aggressive OEM task killers (Samsung's included) that a
# WakeLock alone does not prevent.
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,com.termux.permission.RUN_COMMAND,INTERNET,WAKE_LOCK,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS

# Reverted: lowering android.api to 28 to dodge scoped storage also drags
# compileSdkVersion down with it in this p4a version (they're the same
# value here, not independent) -- Kivy's bundled SDL2 Java glue
# (HIDDeviceManager.java) references Manifest.permission.BLUETOOTH_CONNECT
# (API 31+), which then fails to compile. Storage access is instead
# handled in Python via Android's Storage Access Framework / MediaStore
# (see androidstorage4kivy usage in main.py) rather than a raw path read.
android.api = 33
android.minapi = 24
android.ndk = 25b
# Single arch keeps the first CI build faster/less failure-prone.
# Add ",armeabi-v7a" back once a build succeeds if you need 32-bit devices too.
android.archs = arm64-v8a
android.allow_backup = True
android.accept_sdk_license = True

# Patches android:requestLegacyExternalStorage="true" into the built
# AndroidManifest.xml (see p4a_hook.py for why: it's the only way to
# restore plain-path read/write access to /sdcard on the Android 10 test
# device without an unmerged upstream p4a PR).
p4a.hook = %(source.dir)s/p4a_hook.py

# We use an *explicit* intent (setClassName) to reach Termux's
# RunCommandService, which is exempt from Android 11+ package-visibility
# filtering -- so no <queries> manifest entry is needed here.

[buildozer]

log_level = 2
warn_on_root = 1
