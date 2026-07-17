# Credits & Third-Party Notices

This skill is original orchestration code (ZIP expansion, parallel scraping,
merge/dedupe, and the decoupled email extractor). It builds on the following
third-party components, used as dependencies:

## Scraping engine — gosom/google-maps-scraper

The Google Maps scraping is performed by the open-source
[gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper),
pulled and run as a Docker image (`gosom/google-maps-scraper:latest-rod`). Its
source is **not** redistributed here; this skill only invokes the published image.

Licensed under the MIT License:

```
MIT License

Copyright (c) 2023 Georgios Komninos

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## ZIP code dataset — GeoNames

`data/us_zipcodes.csv` is derived from [GeoNames](https://www.geonames.org/)
postal-code data, licensed under
[Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/).
Columns were reduced to `zip, city, state, state_abbr, lat, lng`.
