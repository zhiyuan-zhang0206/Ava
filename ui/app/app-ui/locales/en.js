// Ava app UI locale copy — en.
// Loaded by index.html; the page picks the matching one via navigator.language.
// English strings are also the static HTML defaults; this file is the canonical
// copy so the page can switch without touching markup.
window.AVA_APP_I18N = window.AVA_APP_I18N || {};
window.AVA_APP_I18N['en'] =
{
  "connectTitle": "Connect to Ava",
  "connectIntro": "Enter the Ava server address; the secret is optional — with it you land already signed in",
  "serverLabel": "Server address",
  "serverPlaceholder": "host or http://host:port",
  "secretLabel": "Cluster secret",
  "optionalTag": "(optional)",
  "secretHint": "Leave empty to sign in on the web login page after connecting",
  "advancedTitle": "Advanced options",
  "gatewayLabel": "Gateway address override",
  "gatewayPlaceholder": "Derived from the server address by default",
  "backgroundLabel": "Run in background",
  "notificationsLabel": "Notifications",
  "connectButton": "Connect",
  "cancelButton": "Cancel",
  "connectingTitle": "Connecting…",
  "stepProbe": "Probe the console",
  "stepExchange": "Exchange sessions",
  "stepLoad": "Load the UI",
  "countdown": "waiting at most {seconds} more seconds",
  "unreachableTitle": "Connection failed",
  "retryButton": "Retry",
  "changeServerButton": "Change address",
  "showLabel": "Show",
  "hideLabel": "Hide",
  "failureUnreachable": "This device cannot reach the address - check the private network, the LAN, and that the address was copied correctly",
  "failureHttp": "The server is up but the console is not - retry shortly",
  "failureAuth": "Wrong secret - re-enter it or clear it to fall through to the web login page",
  "failureUpdateWindow": "The server may be upgrading or rolling back; retry shortly.",
  "timeoutMessage": "Connection timed out - please retry."
};
