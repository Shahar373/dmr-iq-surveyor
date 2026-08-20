echo "=============== A. What SoapySDR does the venv actually load ==============="
python3 - <<'PY'
import SoapySDR, ctypes, os
print("API :", SoapySDR.getAPIVersion())
print("ABI :", SoapySDR.getABIVersion())
print("root:", SoapySDR.getRootPath())
print("search paths:", list(SoapySDR.listSearchPaths()))
print()
print("--- modules libSoapySDR sees, and whether each LOADED cleanly ---")
for m in SoapySDR.listModules():
    err = SoapySDR.getLoaderResult(m)
    ver = SoapySDR.getModuleVersion(m)
    print(f"  {m}\n      version={ver!r} loader_error={err!r}")
PY

echo
echo "=============== B. What enumerate actually returned ==============="
python3 - <<'PY'
import SoapySDR
res = SoapySDR.Device.enumerate({"driver": "sdrplay"})
print("count:", len(res))
for r in res:
    print("  kwargs:", dict(r))
print()
print("unfiltered enumerate (every driver visible to this process):")
for r in SoapySDR.Device.enumerate():
    print("  ", dict(r))
PY

echo
echo "=============== C. Three ways to make the device ==============="
python3 - <<'PY'
import SoapySDR
def attempt(label, fn):
    try:
        d = fn()
        print(f"  {label}: OK -> {d.getDriverKey()} / {d.getHardwareKey()}")
        del d
    except Exception as e:
        print(f"  {label}: FAIL -> {type(e).__name__}: {e}")

attempt("Device({'driver':'sdrplay'})", lambda: SoapySDR.Device({"driver": "sdrplay"}))
attempt("Device('driver=sdrplay')     ", lambda: SoapySDR.Device("driver=sdrplay"))
try:
    args = dict(SoapySDR.Device.enumerate({"driver": "sdrplay"})[0])
    attempt(f"Device({args})", lambda: SoapySDR.Device(args))
except Exception as e:
    print("  could not build args from enumerate:", e)
attempt("Device({}) no filter        ", lambda: SoapySDR.Device({}))
PY

echo
echo "=============== D. The C++ side, for comparison ==============="
SoapySDRUtil --info 2>&1 | sed -n '1,60p'
