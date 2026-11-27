#!/bin/sh
set -eu
if [ "$#" -ne 1 ]; then
    echo 'Usage: sh build_bridge.sh /path/to/data_sts2_windows_x86_64' >&2
    exit 1
fi
reference_dir=$(cd "$1" && pwd -P)
for reference in sts2.dll GodotSharp.dll 0Harmony.dll; do
    if [ ! -f "$reference_dir/$reference" ]; then
        echo "Missing game build reference: $reference" >&2
        exit 1
    fi
done
cd "$(dirname "$0")"
dotnet build Bridge/Bridge.csproj -c Release "-p:GameReferenceDir=$reference_dir"
cp Bridge/bin/Release/net9.0/Bridge.dll worker/WebCoop.dll
