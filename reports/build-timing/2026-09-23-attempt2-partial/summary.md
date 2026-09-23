# Nuitka C-compile breakdown

Source: `packaging\build\entry_point.build` -- 1 ccache log(s), 2217 invocations, 2217 `.o` files, 3079 `.c` files.
First invocation: 2026-09-23T10:34:12.233178; last log timestamp: 2026-09-23T15:13:50.747747.

`wall_s` = ccache/gcc time + idle gap before the invocation (Scons scanning/scheduling) -- with --jobs=1 these sum to the C-compile stage wall-clock. `hits` = ccache hits (object restored, near-zero gcc time).

| package | files | wall min | % | gcc min | hits | misses | in-flight | test files | test wall min | .o-gap min | not yet compiled (files / MB .c) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| scipy | 393 | 80.1 | 28.6 | 78.5 | 0 | 393 | 0 | 117 | 41.2 | 80.1 | 599 / 646.3 |
| numpy | 356 | 58.2 | 20.8 | 57.7 | 0 | 356 | 0 | 204 | 43.1 | 58.2 | 0 / 0.0 |
| onnxruntime | 308 | 30.1 | 10.8 | 29.8 | 0 | 308 | 0 | 0 | 0.0 | 30.1 | 0 / 0.0 |
| narwhals | 150 | 16.2 | 5.8 | 16.0 | 0 | 150 | 0 | 0 | 0.0 | 16.2 | 0 / 0.0 |
| pydantic | 90 | 14.5 | 5.2 | 14.4 | 0 | 90 | 0 | 0 | 0.0 | 14.5 | 0 / 0.0 |
| faiss | 24 | 13.4 | 4.8 | 13.4 | 7 | 17 | 0 | 0 | 0.0 | 13.4 | 0 / 0.0 |
| pypdf | 55 | 9.3 | 3.3 | 9.2 | 0 | 55 | 0 | 0 | 0.0 | 9.3 | 0 / 0.0 |
| mcp | 93 | 9.0 | 3.2 | 8.9 | 0 | 93 | 0 | 0 | 0.0 | 9.0 | 0 / 0.0 |
| fastapi | 48 | 8.2 | 2.9 | 8.1 | 0 | 48 | 0 | 0 | 0.0 | 8.2 | 0 / 0.0 |
| reclaim | 64 | 8.0 | 2.8 | 7.9 | 0 | 64 | 0 | 0 | 0.0 | 8.0 | 0 / 0.0 |
| jinja2 | 21 | 4.2 | 1.5 | 4.1 | 0 | 21 | 0 | 1 | 0.1 | 4.2 | 0 / 0.0 |
| google | 32 | 3.7 | 1.3 | 3.7 | 0 | 32 | 0 | 0 | 0.0 | 3.7 | 0 / 0.0 |
| httpcore | 31 | 3.5 | 1.3 | 3.5 | 0 | 31 | 0 | 0 | 0.0 | 3.5 | 0 / 0.0 |
| lightgbm | 9 | 2.6 | 0.9 | 2.6 | 0 | 9 | 0 | 0 | 0.0 | 2.6 | 0 / 0.0 |
| httpx | 22 | 2.6 | 0.9 | 2.5 | 0 | 22 | 0 | 0 | 0.0 | 2.6 | 0 / 0.0 |
| pydantic_settings | 22 | 2.4 | 0.9 | 2.4 | 0 | 22 | 0 | 0 | 0.0 | 2.4 | 0 / 0.0 |
| pywt | 18 | 2.2 | 0.8 | 2.2 | 0 | 18 | 0 | 0 | 0.0 | 2.2 | 0 / 0.0 |
| rapidocr_onnxruntime | 20 | 1.7 | 0.6 | 1.7 | 0 | 20 | 0 | 0 | 0.0 | 1.7 | 0 / 0.0 |
| pydantic_core | 2 | 1.5 | 0.5 | 1.5 | 0 | 2 | 0 | 0 | 0.0 | 1.5 | 0 / 0.0 |
| jsonschema | 10 | 1.2 | 0.4 | 1.2 | 0 | 10 | 0 | 0 | 0.0 | 1.2 | 0 / 0.0 |
| h11 | 11 | 1.0 | 0.4 | 1.0 | 0 | 11 | 0 | 0 | 0.0 | 1.0 | 0 / 0.0 |
| flatbuffers | 9 | 0.8 | 0.3 | 0.8 | 0 | 9 | 0 | 0 | 0.0 | 0.8 | 0 / 0.0 |
| pywin | 9 | 0.7 | 0.2 | 0.7 | 0 | 9 | 0 | 0 | 0.0 | 0.7 | 0 / 0.0 |
| referencing | 6 | 0.6 | 0.2 | 0.6 | 0 | 6 | 0 | 0 | 0.0 | 0.6 | 0 / 0.0 |
| python_multipart | 4 | 0.5 | 0.2 | 0.5 | 0 | 4 | 0 | 0 | 0.0 | 0.5 | 0 / 0.0 |
| idna | 6 | 0.4 | 0.1 | 0.4 | 0 | 6 | 0 | 0 | 0.0 | 0.4 | 0 / 0.0 |
| httpx_sse | 5 | 0.3 | 0.1 | 0.3 | 0 | 5 | 0 | 0 | 0.0 | 0.3 | 0 / 0.0 |
| PIL | 97 | 0.3 | 0.1 | 0.2 | 97 | 0 | 0 | 0 | 0.0 | 0.3 | 0 / 0.0 |
| httptools | 5 | 0.3 | 0.1 | 0.3 | 0 | 5 | 0 | 0 | 0.0 | 0.3 | 0 / 0.0 |
| docx | 95 | 0.3 | 0.1 | 0.2 | 95 | 0 | 0 | 0 | 0.0 | 0.3 | 0 / 0.0 |
| cv2 | 30 | 0.2 | 0.1 | 0.2 | 28 | 2 | 0 | 0 | 0.0 | 0.2 | 0 / 0.0 |
| markupsafe | 2 | 0.2 | 0.1 | 0.2 | 0 | 2 | 0 | 0 | 0.0 | 0.2 | 0 / 0.0 |
| imagehash | 1 | 0.2 | 0.1 | 0.2 | 0 | 1 | 0 | 0 | 0.0 | 0.2 | 0 / 0.0 |
| anyio | 45 | 0.1 | 0.1 | 0.1 | 45 | 0 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| multipart | 2 | 0.1 | 0.0 | 0.1 | 0 | 2 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| jsonschema_specifications | 2 | 0.1 | 0.0 | 0.1 | 0 | 2 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| pyclipper | 2 | 0.1 | 0.0 | 0.1 | 0 | 2 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| <nuitka> | 3 | 0.1 | 0.0 | 0.1 | 2 | 1 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| datasketch | 21 | 0.1 | 0.0 | 0.0 | 21 | 0 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| pywintypes | 1 | 0.1 | 0.0 | 0.1 | 0 | 1 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| multiprocessing-postLoad | 1 | 0.1 | 0.0 | 0.1 | 0 | 1 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| lxml | 1 | 0.1 | 0.0 | 0.1 | 0 | 1 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| multiprocessing-preLoad | 1 | 0.1 | 0.0 | 0.1 | 0 | 1 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| cryptography | 18 | 0.1 | 0.0 | 0.0 | 18 | 0 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| rpds | 1 | 0.1 | 0.0 | 0.1 | 0 | 1 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| click | 16 | 0.0 | 0.0 | 0.0 | 16 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| pythoncom | 1 | 0.0 | 0.0 | 0.0 | 0 | 1 | 0 | 0 | 0.0 | 0.1 | 0 / 0.0 |
| pywin32_system32 | 1 | 0.0 | 0.0 | 0.0 | 0 | 1 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| cffi | 13 | 0.0 | 0.0 | 0.0 | 13 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| attr | 13 | 0.0 | 0.0 | 0.0 | 13 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| attrs | 6 | 0.0 | 0.0 | 0.0 | 6 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| colorama | 6 | 0.0 | 0.0 | 0.0 | 6 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| dotenv | 4 | 0.0 | 0.0 | 0.0 | 4 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| certifi | 2 | 0.0 | 0.0 | 0.0 | 2 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| annotated_doc | 2 | 0.0 | 0.0 | 0.0 | 2 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| commctrl | 1 | 0.0 | 0.0 | 0.0 | 1 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| annotated_types | 1 | 0.0 | 0.0 | 0.0 | 1 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| __parents_main__-preLoad | 1 | 0.0 | 0.0 | 0.0 | 1 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| blake3 | 1 | 0.0 | 0.0 | 0.0 | 1 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| anyio-preLoad | 1 | 0.0 | 0.0 | 0.0 | 1 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| __main__ | 1 | 0.0 | 0.0 | 0.0 | 1 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| __parents_main__ | 1 | 0.0 | 0.0 | 0.0 | 1 | 0 | 0 | 0 | 0.0 | 0.0 | 0 / 0.0 |
| send2trash | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 7 / 0.8 |
| shapely | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 32 / 9.1 |
| sse_starlette | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 4 / 1.3 |
| starlette | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 34 / 17.0 |
| structlog | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 21 / 9.0 |
| tokenizers | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 16 / 3.5 |
| tqdm | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 13 / 5.2 |
| typing_extensions | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 1 / 4.6 |
| typing_inspection | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 3 / 1.2 |
| uvicorn | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 41 / 13.4 |
| watchfiles | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 5 / 1.6 |
| websockets | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 25 / 11.1 |
| win32com | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 17 / 11.5 |
| win32con | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 1 / 4.7 |
| win32evtlogutil | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 1 / 0.2 |
| win32ui-preLoad | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 1 / 0.0 |
| windows_toasts | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 9 / 2.6 |
| winerror | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 1 / 9.3 |
| winrt | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 14 / 2.3 |
| yaml | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | 17 / 11.4 |

Total wall: 279.6 min.

Top 25 files by wall time:

| source | result | wall s |
|---|---|---:|
| module.faiss.swigfaiss.c | cache_miss | 692 |
| module.numpy._core.tests.test_multiarray.c | cache_miss | 213 |
| module.scipy.integrate._lebedev.c | cache_miss | 195 |
| module.scipy.linalg.tests.test_decomp_update.c | cache_miss | 153 |
| module.scipy.linalg.tests.test_basic.c | cache_miss | 145 |
| module.scipy.linalg.tests.test_decomp.c | cache_miss | 131 |
| module.numpy._core.tests.test_umath.c | cache_miss | 124 |
| module.scipy.linalg.tests.test_lapack.c | cache_miss | 124 |
| module.scipy.interpolate.tests.test_bsplines.c | cache_miss | 120 |
| module.numpy.ma.tests.test_core.c | cache_miss | 89 |
| module.pydantic_core.core_schema.c | cache_miss | 87 |
| module.fastapi.routing.c | cache_miss | 85 |
| module.numpy.lib.tests.test_function_base.c | cache_miss | 82 |
| module.numpy._core.tests.test_numeric.c | cache_miss | 80 |
| module.fastapi.applications.c | cache_miss | 79 |
| module.scipy.interpolate.tests.test_interpolate.c | cache_miss | 70 |
| module.pypdf._writer.c | cache_miss | 68 |
| module.numpy._core.tests.test_ufunc.c | cache_miss | 63 |
| module.scipy.linalg.tests.test_fblas.c | cache_miss | 63 |
| module.mcp.types.c | cache_miss | 63 |
| module.fastapi.openapi.models.c | cache_miss | 59 |
| module.numpy._core.tests.test_nditer.c | cache_miss | 59 |
| module.pydantic.v1.errors.c | cache_miss | 59 |
| module.jinja2.nodes.c | cache_miss | 58 |
| module.lightgbm.basic.c | cache_miss | 57 |
