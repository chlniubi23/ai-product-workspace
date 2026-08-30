import { NextRequest, NextResponse } from "next/server";
import { legacyRouteAliases } from "@/lib/navigation";

// Server-side auth boundary (BUG-001): business routes must not be served to
// unauthenticated requests. The API bearer token lives in localStorage, so the
// client mirrors a session marker cookie at login; without it we redirect to
// /login before any workspace UI HTML is returned.
const PUBLIC_PATHS = ["/login"];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const isPublic = PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`));
  const hasSession = request.cookies.get("apw_session")?.value === "1";

  if (!isPublic && !hasSession) {
    const login = request.nextUrl.clone();
    login.pathname = "/login";
    login.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname)}`;
    return NextResponse.redirect(login, { status: 302 });
  }
  if (pathname === "/login" && hasSession) {
    const home = request.nextUrl.clone();
    home.pathname = "/";
    home.search = "";
    return NextResponse.redirect(home, { status: 302 });
  }
  // V1.0 URLs and bookmarks resolve to their refactored destination.
  const alias = legacyRouteAliases[pathname];
  if (alias) {
    const target = request.nextUrl.clone();
    target.pathname = alias;
    return NextResponse.redirect(target, { status: 308 });
  }
  return NextResponse.next();
}

export const config = {
  // Guard everything except Next.js internals and static assets.
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|css|js|map)$).*)",
  ],
};
