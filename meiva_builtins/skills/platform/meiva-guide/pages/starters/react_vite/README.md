# React starter

Copy this directory to a task-owned project directory. The installed source is
`$MEIVA_HOME/skills/meiva-guide/pages/starters/react_vite/`. Edit the frontend and
install its dependencies with `npm install`. Add widget dependencies only when
using that widget; its README lists them.

Build with `npm run build`, then publish the generated `dist/` directory using
`meiva.ui.serve`. Read `meiva.help(meiva.ui)` for port and lifecycle contracts.
The starter uses `base: './'` so generated asset links work beneath Meiva's page
URL. Keep media links relative as well, for example `video.mp4`, not `/video.mp4`.

Use the Vite development server for local development. The Meiva page proxy is a
GET proxy and does not forward HMR WebSockets or application POST routes;
registering a development server is not a promise of working HMR. Publish the
static build for the user. The templates do not supply an application backend.
For Meiva user input, follow the [reply resource](../../widgets/meiva_reply/README.md).
