"""Server-only Supabase Auth adapter; trusted SQL assignment grants the role."""

from .transport import RemoteError, request_json


class SupabaseAuth:
    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self):
        return bool(self.settings.supabase_url and self.settings.supabase_anon_key)

    def _call(self, method, path, token=None, body=None, admin_key=None):
        if not self.configured:
            raise RemoteError(503)
        key = admin_key or self.settings.supabase_anon_key
        headers = {"apikey": key}
        # Modern project API keys are not JWTs. Only a user token or a legacy
        # JWT API key belongs in Authorization; the gateway handles apikey.
        if token or not key.startswith(("sb_publishable_", "sb_secret_")):
            headers["Authorization"] = "Bearer " + (token or key)
        return request_json(
            method,
            self.settings.supabase_url + "/auth/v1/" + path,
            headers,
            body,
        )

    def login(self, email, password):
        result = self._call("POST", "token?grant_type=password", body={"email": email, "password": password})
        if (
            not isinstance(result.get("access_token"), str)
            or not result["access_token"]
            or not isinstance(result.get("user"), dict)
            or not isinstance(result["user"].get("id"), str)
        ):
            raise RemoteError(502)
        return result

    def validate(self, token):
        result = self._call("GET", "user", token=token)
        if not result.get("id"):
            raise RemoteError(401)
        return result

    def logout(self, token):
        self._call("POST", "logout?scope=local", token=token)

    def invite(self, email, service_key):
        # This is called only by explicit trusted CLI; no public signup/provisioning endpoint.
        return self._call("POST", "invite", body={"email": email}, admin_key=service_key)
