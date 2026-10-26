# Repository artwork

| Asset | Purpose |
|---|---|
| [videotag-banner.svg](videotag-banner.svg) | Editable vector banner used at the top of the repository README; 1280 × 480 |
| [dashboard.png](dashboard.png) | Actual dashboard UI with six fictional sample videos; captured from `main` at `7e0995c` |
| [social-preview.png](social-preview.png) | 1280 × 640 PNG for GitHub's repository social preview |

The banner is an illustration, not a screenshot of Synology Photos. The dashboard uses fictional names, filenames, descriptions, scores, counts, and timings. It is not a performance or accuracy benchmark, and contains no personal library data.

To update the dashboard image, serve the assets in `videotag/gui_assets/` with synthetic responses for the `/api/` endpoints. Use a 1280-pixel-wide browser in dark mode, open **Videos**, expand the first result, and capture the full page. Keep the sample-data caption in the main README. Do not connect the preview to a real NAS or credential store.

The social image uses the banner at its native size on a `#152536` background, centered in a 1280 × 640 canvas, with `github.com/AIPris/videotag` below it. Keep exports below 1 MB.

To activate it, open the repository's **Settings → General → Social preview → Edit → Upload an image** and select `social-preview.png`. GitHub permits a new preview upload for a public repository, or a private one that already had a preview; previews are shared only for public repositories. [GitHub's social preview documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview)
