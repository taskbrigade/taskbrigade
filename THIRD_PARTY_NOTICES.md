# Third-Party Notices

TaskBrigade bundles the third-party assets below under `app/static/vendor/`.
They are not covered by the Apache-2.0 license of this project. Each notice
matches the copy that is actually in the tree (version taken from the file
header or from the upstream tag for that file).

Python dependencies declared in `pyproject.toml` and pinned in `uv.lock` are
not vendored. They are installed from PyPI and stay under their own licenses.

## Pico CSS 2.0.6

- Path: `app/static/vendor/pico.min.css`
- Used from: `app/templates/base.html`
- Project: <https://github.com/picocss/pico> (v2.0.6)
- License: MIT
- Copyright (c) 2019-2024 Pico
- Bundled header: `Pico CSS v2.0.6 (https://picocss.com) — Copyright 2019-2024 - Licensed under MIT`

```
MIT License

Copyright (c) 2019-2024 Pico

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

## htmx 2.0.4

- Path: `app/static/vendor/htmx.min.js`
- Used from: `app/templates/base.html`
- Project: <https://github.com/bigskysoftware/htmx> (tag v2.0.4)
- License: Zero-Clause BSD (0BSD)
- The minified file has no license header. Its version string is `2.0.4`.
  The license text below is the `LICENSE` file from that tag. It is 0BSD,
  not the 2-clause BSD license.

```
Zero-Clause BSD

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

## Sortable 1.15.6

- Path: `app/static/vendor/Sortable.min.js`
- Used from: `app/templates/kanban.html` (drag and drop on the board)
- Project: <https://github.com/SortableJS/Sortable> (1.15.6)
- License: MIT
- Copyright (c) 2019 All contributors to Sortable
- Bundled header: `Sortable 1.15.6 - MIT | git://github.com/SortableJS/Sortable.git`

```
MIT License

Copyright (c) 2019 All contributors to Sortable

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

