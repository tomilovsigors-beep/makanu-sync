# Outfish Makanu Sync

Small Render service for connecting Outfish with the Makanu B2B portal.

## Security

Never commit Makanu credentials or API keys to GitHub.
Keep secrets only in Render Environment Variables.

## Deploy to Render

1. Push this repository to GitHub.
2. In Render choose **New > Blueprint** and select the repository.
3. Render will read `render.yaml`.
4. Set `BRIDGE_API_KEY` in Render to a long random value.
5. Deploy.

## Test

Open:
- `/health`
- `/probe` with HTTP header `X-API-Key: <BRIDGE_API_KEY>`

`/probe` only checks that Render can open the Makanu portal and reports
whether a password field is present. It does not store credentials.

## Next step

After `/probe` works, add Makanu authentication using Render Environment
Variables and then expose only the supplier data needed for Outfish.
