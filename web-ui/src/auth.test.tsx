import { StrictMode, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGuard, AuthProvider, useAuth } from "./auth";
import type { HostAuthAdapter } from "./auth-adapter";

const mocks = vi.hoisted(() => ({ load: vi.fn(), config: vi.fn() }));
vi.mock("./auth-adapter", () => ({ loadHostAdapter: mocks.load }));
vi.mock("./config", () => ({ getRuntimeConfig: mocks.config, uiRootUrl: () => new URL("http://localhost/ui/") }));

let listener: (key: string | null) => void;
let adapter: HostAuthAdapter;
let unsubscribe: ReturnType<typeof vi.fn>;

function PrivateView() {
  const [memory, setMemory] = useState("");
  const { logout, busy } = useAuth();
  return <>
    <label>Memory draft<input aria-label="Memory draft" value={memory} onChange={e => setMemory(e.target.value)} /></label>
    <button disabled={busy} onClick={() => void logout()}>Sign out</button>
  </>;
}
function App() {
  return <StrictMode><AuthProvider><AuthGuard><PrivateView /></AuthGuard></AuthProvider></StrictMode>;
}

beforeEach(() => {
  vi.clearAllMocks();
  unsubscribe = vi.fn();
  adapter = {
    sessionKey: () => "session_one",
    subscribe: (callback) => { listener = callback; return unsubscribe; },
    login: vi.fn(async () => {}), logout: vi.fn(async () => {}), getToken: vi.fn(async () => "token"),
  };
  mocks.load.mockResolvedValue(adapter);
  mocks.config.mockResolvedValue({ auth: { mode: "adapter" } });
});
afterEach(cleanup);

describe("host authentication lifecycle", () => {
  it("subscribes once under StrictMode and discards private state on account changes", async () => {
    const view = render(<App />);
    const input = await screen.findByRole("textbox");
    fireEvent.change(input, { target: { value: "private draft" } });
    act(() => listener("session_two"));
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("");
    expect(mocks.load).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("removes private content when another tab signs out", async () => {
    render(<App />);
    await screen.findByRole("textbox");
    act(() => listener(null));
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy();
  });

  it("waits for provider logout and prevents duplicate submissions", async () => {
    let complete!: () => void;
    adapter.logout = vi.fn(() => new Promise<void>(resolve => { complete = resolve; }));
    render(<App />);
    const button = await screen.findByRole("button", { name: "Sign out" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(adapter.logout).toHaveBeenCalledTimes(1);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("textbox")).not.toBeNull();
    await act(async () => complete());
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("keeps the session and reports a failed logout without leaking provider errors", async () => {
    adapter.logout = vi.fn(async () => { throw new Error("sensitive provider response"); });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch("Sign-out could not complete"));
    expect(screen.queryByRole("textbox")).not.toBeNull();
    expect(screen.queryByText(/sensitive provider/)).toBeNull();
  });

  it("starts one login and reports a retryable failure", async () => {
    adapter.sessionKey = () => null;
    adapter.login = vi.fn(async () => { throw new Error("provider failure"); });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in" }));
    await screen.findByText("Sign-in could not start. Please try again.");
    expect(adapter.login).toHaveBeenCalledWith("http://localhost/ui/");
  });
});
