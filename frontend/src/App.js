import "@/App.css";
import { useEffect, useState } from "react";
import axios from "axios";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import Dashboard from "@/components/Dashboard";
import Login from "@/components/Login";
import { Toaster } from "@/components/ui/sonner";
import { Loader2 } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

function App() {
  const [authed, setAuthed] = useState(null); // null=checking, true, false

  useEffect(() => {
    // 401 anywhere -> force re-login
    const id = axios.interceptors.response.use(
      (r) => r,
      (err) => {
        if (err.response?.status === 401) {
          localStorage.removeItem("rnb_token");
          delete axios.defaults.headers.common["Authorization"];
          setAuthed(false);
        }
        return Promise.reject(err);
      }
    );
    const token = localStorage.getItem("rnb_token");
    if (!token) {
      setAuthed(false);
      return () => axios.interceptors.response.eject(id);
    }
    axios.defaults.headers.common["Authorization"] = `Bearer ${token}`;
    axios
      .get(`${API}/auth/me`)
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false));
    return () => axios.interceptors.response.eject(id);
  }, []);

  const logout = () => {
    localStorage.removeItem("rnb_token");
    delete axios.defaults.headers.common["Authorization"];
    setAuthed(false);
  };

  if (authed === null) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <Loader2 className="h-6 w-6 text-red-500 animate-spin" />
      </div>
    );
  }

  return (
    <div className="App">
      {authed ? (
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Dashboard onLogout={logout} />} />
          </Routes>
        </BrowserRouter>
      ) : (
        <Login onLogin={() => setAuthed(true)} />
      )}
      <Toaster position="bottom-right" />
    </div>
  );
}

export default App;
