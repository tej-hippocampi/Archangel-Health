"use client";

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { User } from "@/lib/auth-api";
import * as authApi from "@/lib/auth-api";

const TOKEN_KEY = "archangel_auth_token";

type AuthState = {
  user: User | null;
  token: string | null;
  loading: boolean;
  error: string | null;
  pendingVerificationEmail: string | null;
};

type AuthContextValue = AuthState & {
  login: (email: string, password: string) => Promise<void>;
  register: (
    email: string,
    password: string,
    name?: string,
    phone?: string,
    smsConsentOptIn?: boolean
  ) => Promise<User>;
  verifyEmail: (email: string, code: string) => Promise<void>;
  resendVerification: (email: string) => Promise<void>;
  logout: () => void;
  clearError: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(() =>
    typeof window !== "undefined" ? localStorage.getItem(TOKEN_KEY) : null
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pendingVerificationEmail, setPendingVerificationEmail] = useState<string | null>(null);

  const applyToken = useCallback((newToken: string | null) => {
    if (typeof window === "undefined") return;
    if (newToken) {
      localStorage.setItem(TOKEN_KEY, newToken);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
    setToken(newToken);
  }, []);

  useEffect(() => {
    if (!token) {
      setUser(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    authApi.getMe(token).then((u) => {
      if (!cancelled) {
        setUser(u);
      }
      if (!cancelled) setLoading(false);
    }).catch(() => {
      if (!cancelled) {
        applyToken(null);
        setUser(null);
      }
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [token, applyToken]);

  const login = useCallback(async (email: string, password: string) => {
    setError(null);
    setPendingVerificationEmail(null);
    try {
      const data = await authApi.login(email, password);
      if (authApi.isVerificationRequired(data)) {
        const message = "Please verify your email before signing in.";
        setPendingVerificationEmail(data.email);
        setError(message);
        throw new Error(message);
      }
      applyToken(data.access_token);
      setUser(data.user);
    } catch (e) {
      if (!(e instanceof Error) || e.message !== "Please verify your email before signing in.") {
        setError(e instanceof Error ? e.message : "Sign in failed");
      }
      throw e;
    }
  }, [applyToken]);

  const register = useCallback(
    async (
      email: string,
      password: string,
      name?: string,
      phone?: string,
      smsConsentOptIn?: boolean
    ) => {
      setError(null);
      try {
        const data = await authApi.register(email, password, name, phone, smsConsentOptIn);
        applyToken(data.access_token);
        setUser(data.user);
        if (!data.user.email_verified) {
          setPendingVerificationEmail(data.user.email);
        }
        return data.user;
      } catch (e) {
        setError(e instanceof Error ? e.message : "Registration failed");
        throw e;
      }
    },
    [applyToken]
  );

  const verifyEmail = useCallback(async (email: string, code: string) => {
    setError(null);
    try {
      await authApi.verifyEmailOtp(email, code);
      setPendingVerificationEmail(null);
      setUser((prev) => (prev && prev.email.toLowerCase() === email.toLowerCase()
        ? { ...prev, email_verified: true }
        : prev));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Verification failed");
      throw e;
    }
  }, []);

  const resendVerification = useCallback(async (email: string) => {
    setError(null);
    try {
      await authApi.resendVerification(email);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not resend the verification email");
      throw e;
    }
  }, []);

  const logout = useCallback(() => {
    applyToken(null);
    setUser(null);
    setError(null);
    setPendingVerificationEmail(null);
  }, [applyToken]);

  const clearError = useCallback(() => setError(null), []);

  const value: AuthContextValue = {
    user,
    token,
    loading,
    error,
    pendingVerificationEmail,
    login,
    register,
    verifyEmail,
    resendVerification,
    logout,
    clearError,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
