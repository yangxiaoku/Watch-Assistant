import type { ProwlarrSettingsState, ProwlarrValidationState } from "./types";

/**
 * Frontend-only seam for the future Watch Assistant Prowlarr settings route.
 *
 * The route and wire fields are intentionally not guessed here. The parent
 * application can inject an adapter once the server contract is frozen.
 * The adapter receives only redacted status data; credentials never belong here.
 */
export interface ProwlarrSettingsClient {
  settings(): Promise<ProwlarrSettingsState>;
  validateConnection(): Promise<ProwlarrValidationState>;
}

export const pendingProwlarrSettingsClient: ProwlarrSettingsClient = {
  async settings() {
    return {
      enabled: false,
      configured: false,
      status: "unsupported",
      status_zh: "后端接口待对接",
      last_checked_at: null,
      last_success_at: null,
    };
  },
  async validateConnection() {
    return {
      status: "unsupported",
      message_zh: "Prowlarr 后端验证接口待对接",
      checked_at: null,
    };
  },
};
