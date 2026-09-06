import { authAdapterUrl, uiRootUrl, type RuntimeConfig } from "./config";

/** Host-owned SDK integration. Tokens still cross the normal authenticated API boundary. */
export interface HostAuthAdapter {
  sessionKey(): string | null;
  subscribe(listener: (sessionKey: string | null) => void): () => void;
  getToken(): Promise<string | null>;
  login(returnUrl: string): Promise<void>;
  logout(): Promise<void>;
}

const adapters = new Map<string, Promise<HostAuthAdapter>>();

export function loadHostAdapter(config: RuntimeConfig): Promise<HostAuthAdapter> {
  const url = authAdapterUrl(config).toString();
  const key = url + JSON.stringify(config.auth.adapter_config ?? {});
  let pending = adapters.get(key);
  if (!pending) {
    pending = import(/* @vite-ignore */ url).then(async (module) => {
      const adapter = await module.createAuthAdapter({
        config: config.auth.adapter_config ?? {},
        returnUrl: uiRootUrl().toString(),
      });
      for (const method of ["sessionKey", "subscribe", "getToken", "login", "logout"]) {
        if (typeof adapter?.[method] !== "function") throw new Error("Invalid authentication adapter.");
      }
      return adapter as HostAuthAdapter;
    });
    adapters.set(key, pending);
    void pending.catch(() => adapters.delete(key));
  }
  return pending;
}
