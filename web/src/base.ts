// Sub-path the app is served under ("" at the root, "/ktv" behind a proxy).
// The build uses relative asset URLs, so the bundle's own location tells us:
// <base>/assets/index-xxx.js -> <base>. The dev server always runs at the root.
export const BASE = import.meta.env.DEV
  ? ""
  : new URL(import.meta.url).pathname.replace(/\/assets\/[^/]*$/, "");

/** Browser URL for an app path: url("/api/health") -> "/ktv/api/health". */
export function url(path: string): string {
  return BASE + path;
}

/** App path for the current page, without the sub-path prefix. */
export function appPath(): string {
  const path = location.pathname;
  return BASE && path.startsWith(BASE) ? path.slice(BASE.length) || "/" : path;
}

/** Room (包廂) of the current page: /r/<room>/remote -> "<room>"; old URLs are "main". */
export function room(): string {
  const match = /^\/r\/([a-z0-9][a-z0-9-]{0,15})(\/|$)/.exec(appPath());
  return match ? match[1] : "main";
}

/** The page within its room: /r/vip/remote -> "/remote". */
export function roomPage(): string {
  const path = appPath();
  return path.startsWith("/r/") ? path.replace(/^\/r\/[^/]+/, "") || "/" : path;
}

/** App path of a page in a room: roomUrl("vip", "/display") -> "/r/vip/display". */
export function roomUrl(slug: string, page: string): string {
  return `/r/${slug}${page}`;
}
