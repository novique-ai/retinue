// Settings → Claude account controls (#252). Subscription first: a Claude
// Code (subscription) login is the supported path, and the API-key field is a
// secondary, clearly labelled opt-in to API billing — never the default ask.
import type { ReactElement } from "react";

import type { ProviderAuth } from "./api";

export type AccountRow = ProviderAuth & { login?: string | null };

export const CLAUDE_SUBSCRIPTION_LOGIN = "subscription";
export const CLAUDE_API_KEY_LOGIN = "api_key";

/** One-line description of how Claude is (or is not) signed in. */
export function claudeLoginLabel(acct: AccountRow): string {
  if (acct.status === "ok") {
    return acct.login === CLAUDE_API_KEY_LOGIN
      ? "Using an Anthropic API key (API billing)"
      : "Signed in via Claude subscription";
  }
  if (acct.status === "relogin_required") return "Claude subscription login expired";
  return "Not signed in";
}

export function ClaudeAccountControls(props: {
  acct: AccountRow;
  apiKey: string;
  onApiKeyChange: (value: string) => void;
  onSaveApiKey: () => void;
}): ReactElement {
  const { acct, apiKey, onApiKeyChange, onSaveApiKey } = props;
  if (acct.status === "ok") {
    return (
      <div className="settings-claude" data-testid="claude-login">
        <span className="nav-sub">{claudeLoginLabel(acct)}</span>
      </div>
    );
  }
  return (
    <div className="settings-claude" data-testid="claude-login">
      <span className="nav-sub" data-testid="claude-subscription-help">
        {acct.status === "relogin_required"
          ? "Sign in again with Claude Code on the host to use your Claude subscription."
          : "Sign in with Claude Code on the host to use your Claude subscription."}
      </span>
      <details data-testid="claude-api-key">
        <summary className="nav-sub">Use an API key instead (API billing)</summary>
        <div className="settings-key">
          <input
            type="password"
            placeholder="Anthropic API key (billed per use)"
            value={apiKey}
            onChange={(e) => onApiKeyChange(e.target.value)}
          />
          <button
            className="mini"
            data-testid="claude-api-key-save"
            disabled={!apiKey.trim()}
            onClick={onSaveApiKey}
          >
            Save
          </button>
        </div>
      </details>
    </div>
  );
}
