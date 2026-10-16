# Third-party attributions

## dietly-autopilot (API contract knowledge)

The knowledge of Dietly's private JSON API used in this project — endpoints, headers, the
`company-id` catering-slug rule, the HTTP `490` behaviour and the meal-switch request shape —
is based on the reverse-engineering notes published in:

- **Project:** [jtomaszewski/dietly-autopilot](https://github.com/jtomaszewski/dietly-autopilot)
- **File consulted:** [`docs/dietly-api.md`](https://github.com/jtomaszewski/dietly-autopilot/blob/main/docs/dietly-api.md)
- **License:** MIT

**No code from that project was copied.** Only the documented, factual API contract was
paraphrased into [`docs/api-notes.md`](docs/api-notes.md) and re-implemented from scratch in this
repository. All Python code here is original to `dietly-mcp`.

> **Note (2026-10-16):** those notes (dated 2026-06-10) are now **incomplete** — they describe a
> single `POST /api/auth/login` with no CSRF step. The live panel currently requires a Spring
> Security CSRF token (`XSRF-TOKEN` cookie → `X-XSRF-TOKEN` header), verified against the live SPA
> bundle. See the "Auth" section of [`docs/api-notes.md`](docs/api-notes.md); that part is our own
> observation, not derived from the notes above.

## dietlownik (mobile API — reference only)

- **Project:** [dawidlinek/dietlownik](https://github.com/dawidlinek/dietlownik)
- **File consulted:** [`API.md`](https://github.com/dawidlinek/dietlownik/blob/main/API.md)
- **License:** none declared.

Consulted only to rule out an alternative auth path. It documents the **mobile** API
(`https://aplikacja.dietly.pl/api/mobile/open/...`), whose catalog endpoints are **anonymous**
(no cookies/JWT) and therefore offer no login route for this project. **No code and no content was
copied**; the endpoint list was not reproduced here. `dietly-mcp` uses the panel's cookie session
exclusively.

MIT notice of the referenced project (for attribution purposes):

```
MIT License

Copyright (c) dietly-autopilot contributors

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

## Trademarks

"Dietly" and dietly.pl are the property of their respective owners. This project is an
independent, unofficial client and is not affiliated with or endorsed by Dietly.
