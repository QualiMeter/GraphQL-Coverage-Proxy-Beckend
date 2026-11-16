from starlette.responses import JSONResponse


def route_path(scope) -> str:
    path = scope["path"]
    root = scope.get("root_path", "")
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root):] or "/"
    return path


class AuthBoundaryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = route_path(scope)
        is_auth = path.startswith("/api/auth/")
        is_service_write = path.startswith("/api/services") and scope["method"] in {"POST", "PATCH"}
        is_introspection = path == "/api/schemas/import/introspection" and scope["method"] == "POST"
        is_sdl = path == "/api/schemas/import" and scope["method"] == "POST"
        is_schema_write = path.startswith("/api/schemas/") and scope["method"] == "PATCH"
        if is_auth or is_service_write or is_introspection or is_sdl or is_schema_write:
            body_limit = 1_048_576 if is_sdl else 16_384
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > body_limit:
                    response = JSONResponse({"detail": "Authentication payload too large" if is_auth else "Schema import payload too large" if is_introspection or is_sdl else "Schema payload too large" if is_schema_write else "Service payload too large"}, status_code=413,
                                            headers={"Cache-Control": "no-store"})
                    return await response(scope, receive, send)
                if not message.get("more_body", False):
                    break

            original_receive = receive
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if delivered:
                    return await original_receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            receive = buffered_receive

        async def private_send(message):
            if message["type"] == "http.response.start" and (path.startswith("/api/")
                                                            or path == "/graphql"):
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.extend([(b"cache-control", b"no-store"), (b"vary", b"Cookie, X-Coverage-Api-Key")])
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, private_send)
