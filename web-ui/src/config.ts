export type AuthMode = "none" | "session" | "oidc";

export interface FrontendAuthConfig {
  mode: AuthMode;
  authority: string | null;
  client_id: string | null;
  scope: string;
  token_field: "id_token" | "access_token";
  auto_login: boolean;
}

export interface RuntimeConfig {
  api_base_url: string;
  mcp_base_url: string;
  auth: FrontendAuthConfig;
  product_name: string;
}

const CONFIG_URL = new URL(/* @vite-ignore */ "../config.json", import.meta.url);
let runtimeConfig: Promise<RuntimeConfig> | null = null;

function validate(config: RuntimeConfig): RuntimeConfig {
  if (!config || typeof config !== "object") throw new Error("UI configuration is invalid.");
  if (
    !config.api_base_url ||
    !config.mcp_base_url ||
    !config.auth ||
    !config.product_name
  ) {
    throw new Error("UI configuration is incomplete.");
  }
  if (!["none", "session", "oidc"].includes(config.auth.mode)) {
    throw new Error("UI authentication mode is invalid.");
  }
  return config;
}

export function getRuntimeConfig(): Promise<RuntimeConfig> {
  if (!runtimeConfig) {
    runtimeConfig = fetch(CONFIG_URL, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).then(async (response) => {
      if (!response.ok) throw new Error("Unable to load UI configuration.");
      return validate((await response.json()) as RuntimeConfig);
    });
  }
  return runtimeConfig;
}

export function apiUrl(config: RuntimeConfig, path: string): URL {
  const root = new URL(config.api_base_url.replace(/\/?$/, "/"), CONFIG_URL);
  return new URL(path.replace(/^\//, ""), root);
}

export function mcpConnectionUrl(
  config: RuntimeConfig,
  workspaceSlug: string,
  agentSlug: string,
): URL {
  const root = new URL(
    config.mcp_base_url.replace(/\/?$/, "/"),
    CONFIG_URL,
  );
  return new URL(
    `workspaces/${encodeURIComponent(workspaceSlug)}/agents/${encodeURIComponent(agentSlug)}`,
    root,
  );
}

export function uiRootUrl(): URL {
  return new URL("./", CONFIG_URL);
}
