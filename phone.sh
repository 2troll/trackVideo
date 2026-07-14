#!/bin/sh
# Reconecta la app del teléfono (Android por USB) con el servidor del Mac.
# Úsalo si abres trackVideo en el teléfono y no carga estando con cable.
set -e
if ! adb devices | grep -q "device$"; then
    echo "❌ No veo el teléfono. ¿Cable conectado y depuración USB activada?"
    exit 1
fi
adb reverse tcp:8756 tcp:8756
echo "✅ Túnel USB listo: la app trackVideo del teléfono ya funciona."
adb shell am start -a android.intent.action.VIEW -d "http://localhost:8756/" >/dev/null
