# VoteVault scenario (reference only)

Earlier versions of `main.py` special-cased a Flask app called VoteVault: when a workspace contained
`app.py` and the issue mentioned `export_votes`, the pipeline wrote a hardcoded reproduction test and
applied a hardcoded string replacement as the "patch". That logic has been removed from the production
pipeline and preserved here.

The VoteVault source is not part of this repository, so this scenario cannot run on its own.

## Preserved inputs

- `test_reproduce.py` — the reproduction test the pipeline used to write.
- The scripted fix, which was a text substitution in `app.py`:
  - `from io import StringIO` → `from io import BytesIO, StringIO`
  - `StringIO(output.read())` → `BytesIO(output.getvalue().encode('utf-8'))`

## Running it against a VoteVault checkout

Express the fix as a unified diff against your checkout, then supply both inputs explicitly:

```bash
python main.py --repo /path/to/votevault \
  --issue 1 --title "export_votes raises ValueError" \
  --body "export_votes in app.py crashes: send_file requires BytesIO" \
  --repro-test examples/votevault/test_reproduce.py \
  --patch /path/to/votevault-fix.diff
```

Every gate still applies; nothing is admitted unless the test fails before the patch and passes after it.
