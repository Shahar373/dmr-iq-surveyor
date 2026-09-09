# Vendored map library

`scripts/vendor_leaflet.sh` downloads `leaflet.js` and `leaflet.css` here. The field app tries this
local copy first and falls back to the CDN, so once vendored, losing internet costs only the map
tiles — the measurement points and credible regions still draw.

The downloaded files are gitignored: this repository does not carry third-party minified bundles.
