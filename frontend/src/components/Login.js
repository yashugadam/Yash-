import React, { useState } from "react";
import axios from "axios";
import { Lock, User, TrendingDown, Loader2 } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

export const Login = ({ onLogin }) => {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const { data } = await axios.post(`${API}/auth/login`, { username, password });
      localStorage.setItem("rnb_token", data.token);
      axios.defaults.headers.common["Authorization"] = `Bearer ${data.token}`;
      onLogin(data.username);
    } catch (err) {
      const d = err.response?.data?.detail;
      setError(typeof d === "string" ? d : "Login failed. Check your credentials.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center px-4 font-mono">
      <div className="absolute inset-0 opacity-[0.15]" style={{ backgroundImage: "radial-gradient(circle at 1px 1px, #334155 1px, transparent 0)", backgroundSize: "22px 22px" }} />
      <form onSubmit={submit} className="relative w-full max-w-sm bg-slate-900 border border-slate-800 p-8" data-testid="login-form">
        <div className="flex items-center gap-2 mb-1">
          <TrendingDown className="h-5 w-5 text-red-500" />
          <span className="text-white text-lg font-bold tracking-tight">RENKO NIFTY BOT</span>
        </div>
        <p className="text-slate-500 text-[11px] uppercase tracking-widest mb-8">Live · Real-money · Sign in to continue</p>

        <label className="block text-slate-400 text-[11px] uppercase tracking-widest mb-1">Username</label>
        <div className="flex items-center border border-slate-700 bg-slate-950 mb-4 focus-within:border-red-500 transition-colors">
          <User className="h-4 w-4 text-slate-500 ml-3" />
          <input value={username} onChange={(e) => setUsername(e.target.value)} data-testid="login-username"
            className="w-full bg-transparent text-white px-3 py-2.5 text-sm outline-none" autoCapitalize="none" autoComplete="username" />
        </div>

        <label className="block text-slate-400 text-[11px] uppercase tracking-widest mb-1">Password</label>
        <div className="flex items-center border border-slate-700 bg-slate-950 mb-6 focus-within:border-red-500 transition-colors">
          <Lock className="h-4 w-4 text-slate-500 ml-3" />
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} data-testid="login-password"
            className="w-full bg-transparent text-white px-3 py-2.5 text-sm outline-none" autoComplete="current-password" />
        </div>

        {error && <p className="text-red-400 text-xs mb-4" data-testid="login-error">{error}</p>}

        <button type="submit" disabled={loading} data-testid="login-submit"
          className="w-full bg-red-600 hover:bg-red-500 disabled:opacity-60 text-white py-2.5 text-sm uppercase tracking-widest flex items-center justify-center gap-2 transition-colors">
          {loading ? <><Loader2 className="h-4 w-4 animate-spin" /> Signing in…</> : "Sign In"}
        </button>
      </form>
    </div>
  );
};

export default Login;
