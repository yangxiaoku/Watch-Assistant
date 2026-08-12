import { ref } from "vue";
import { ApiError, focusFirstFieldError, type ApiClient } from "../api";

/**
 * 登录会话状态。login 成功后通过 onAuthenticated 回调触发工作区初始化，
 * 避免 auth 与工作区状态耦合。
 */
export function useAuth(api: ApiClient, onAuthenticated: () => Promise<void>) {
  const username = ref("admin");
  const password = ref("");
  const authenticated = ref(false);
  const loggingIn = ref(false);
  const error = ref("");

  /** 会话恢复：api.me() 成功即视为已登录。 */
  async function checkSession(): Promise<boolean> {
    try {
      await api.me();
      authenticated.value = true;
      return true;
    } catch {
      authenticated.value = false;
      return false;
    }
  }

  async function login() {
    if (loggingIn.value) return;
    error.value = "";
    loggingIn.value = true;
    try {
      await api.login(username.value, password.value);
      authenticated.value = true;
      password.value = "";
      await onAuthenticated();
    } catch (exception) {
      focusFirstFieldError(exception);
      if (exception instanceof ApiError) {
        if (exception.status === 429 || exception.code === "rate_limited") {
          error.value = "尝试次数过多，请一分钟后再试。";
        } else if (exception.status === 401 || exception.code === "invalid_credentials" || exception.code === "unauthorized") {
          error.value = "账号或密码不正确";
        } else {
          error.value = "登录失败，请稍后重试";
        }
      } else {
        error.value = "登录失败";
      }
    } finally {
      loggingIn.value = false;
    }
  }

  return { username, password, authenticated, loggingIn, error, login, checkSession };
}
