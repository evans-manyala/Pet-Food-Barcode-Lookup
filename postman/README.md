# Import into Postman

1. **Collection** — Import `Pet-Food-Barcode-Lookup.postman_collection.json`
2. **Environment** — Import one of:
   - `environments/Local.postman_environment.json` — `http://localhost:8000`
   - `environments/Production.postman_environment.json` — `http://34.133.118.0`

3. Select the environment in the Postman top-right dropdown.

4. Optional: set `stats_token` if your server has `STATS_TOKEN` configured.

5. Share the collection file with testers (they import and pick Production environment).

When HTTPS is live, change `base_url` to `https://api.mindmycat.com`.

Full API reference: [docs/API.md](../docs/API.md)
