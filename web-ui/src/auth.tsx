import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Loader2, LogIn, Shield } from "lucide-react";
import {
  UserManager,
  WebStorageStateStore,
  type User,
} from "oidc-client-ts";

import {
  getRuntimeConfig,
  uiRootUrl,
  type RuntimeConfig,
} from "./config";

interface AuthContextValue {
  config: RuntimeConfig | null;
  authenticated: boolean;
  loading: boolean;
  error: string;
  getToken: () => Promise<string | null>;
  login: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  config: null,
  authenticated: false,
  loading: true,
  error: "",
  getToken: async () => null,
  login: async () => undefined,
  logout: async () => undefined,
});

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

function tokenFor(config: RuntimeConfig, user: User | null): string | null {
  if (!user || user.expired) return null;
  return config.auth.token_field === "id_token"
    ? user.id_token ?? null
    : user.access_token ?? null;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const managerRef = useRef<UserManager | null>(null);
  const automaticLoginStarted = useRef(false);

  useEffect(() => {
    let active = true;
    void getRuntimeConfig()
      .then(async (loaded) => {
        if (!active) return;
        setConfig(loaded);
        if (loaded.auth.mode !== "oidc") {
          setLoading(false);
          return;
        }
        if (!loaded.auth.authority || !loaded.auth.client_id) {
          throw new Error("OIDC configuration is incomplete.");
        }
        const root = uiRootUrl().toString();
        const manager = new UserManager({
          authority: loaded.auth.authority,
          client_id: loaded.auth.client_id,
          redirect_uri: root,
          post_logout_redirect_uri: root,
          response_type: "code",
          scope: loaded.auth.scope,
          automaticSilentRenew: false,
          userStore: new WebStorageStateStore({ store: window.sessionStorage }),
        });
        managerRef.current = manager;
        const params = new URLSearchParams(window.location.search);
        let current: User | null;
        if (params.has("state") && (params.has("code") || params.has("error"))) {
          try {
            current = await manager.signinRedirectCallback();
          } catch (caught) {
            // Consume error callbacks too, and keep stale OAuth parameters out of retries.
            window.history.replaceState(null, "", root);
            throw caught;
          }
          const returnSearch =
            typeof current.state === "string" ? current.state : "";
          window.history.replaceState(
            null,
            "",
            root + (returnSearch.startsWith("?") ? returnSearch : ""),
          );
        } else {
          current = await manager.getUser();
        }
        if (!active) return;
        setUser(current && !current.expired ? current : null);
        setLoading(false);
      })
      .catch((caught: unknown) => {
        if (!active) return;
        setError(caught instanceof Error ? caught.message : String(caught));
        setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const login = useCallback(async () => {
    if (!config || config.auth.mode !== "oidc" || !managerRef.current) return;
    setError("");
    await managerRef.current.signinRedirect({ state: window.location.search });
  }, [config]);

  const logout = useCallback(async () => {
    if (!config || config.auth.mode !== "oidc" || !managerRef.current) return;
    const manager = managerRef.current;
    setError("");
    automaticLoginStarted.current = true;
    try {
      if (await manager.metadataService.getEndSessionEndpoint()) {
        await manager.signoutRedirect();
        return;
      }
      // OIDC providers may omit RP-initiated logout (for example Clerk). Revoke this
      // application's tokens when supported and always clear its local session.
      try {
        if (await manager.metadataService.getRevocationEndpoint()) {
          await manager.revokeTokens(["access_token", "refresh_token"]);
        }
      } finally {
        await manager.removeUser();
        setUser(null);
      }
    } catch {
      setError("Sign-out could not complete at the identity provider. Please try again.");
    }
  }, [config]);

  const getToken = useCallback(async (): Promise<string | null> => {
    if (!config || config.auth.mode !== "oidc") return null;
    const current = await managerRef.current?.getUser();
    const token = tokenFor(config, current ?? null);
    if (!token) setUser(null);
    return token;
  }, [config]);

  const authenticated = Boolean(
    config &&
      (config.auth.mode !== "oidc" || tokenFor(config, user) !== null),
  );

  useEffect(() => {
    if (
      config?.auth.mode === "oidc" &&
      config.auth.auto_login &&
      !loading &&
      !authenticated &&
      !error &&
      !automaticLoginStarted.current
    ) {
      automaticLoginStarted.current = true;
      void login().catch((caught: unknown) => {
        setError(caught instanceof Error ? caught.message : String(caught));
      });
    }
  }, [authenticated, config, error, loading, login]);

  const value = useMemo<AuthContextValue>(
    () => ({
      config,
      authenticated,
      loading,
      error,
      getToken,
      login,
      logout,
    }),
    [authenticated, config, error, getToken, loading, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function AuthGuard({ children }: { children: ReactNode }) {
  const { authenticated, loading, error, login } = useAuth();
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-950">
        <Loader2 className="h-10 w-10 animate-spin text-blue-600" />
      </div>
    );
  }
  if (!authenticated) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 px-4 dark:bg-gray-950">
        <div className="w-full max-w-md rounded-xl border border-gray-200 bg-white p-8 text-center shadow-lg dark:border-gray-800 dark:bg-gray-900">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-blue-100 dark:bg-blue-900/30">
            <Shield className="h-8 w-8 text-blue-600 dark:text-blue-300" />
          </div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Authentication required
          </h1>
          <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
            Sign in through the identity provider configured by this deployment.
          </p>
          {error && (
            <p className="mt-4 rounded-lg bg-red-50 p-3 text-left text-sm text-red-700 dark:bg-red-950/40 dark:text-red-200">
              {error}
            </p>
          )}
          <button
            className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-3 font-medium text-white hover:bg-blue-700"
            onClick={() => void login()}
          >
            <LogIn className="h-5 w-5" />
            Sign in
          </button>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}
