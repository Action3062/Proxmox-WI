import { useAuth } from "./lib/auth.jsx";
import Login from "./components/Login.jsx";
import Dashboard from "./components/Dashboard.jsx";

export default function App() {
  const { user, loading } = useAuth();
  if (loading) {
    return <div className="center muted">Lädt …</div>;
  }
  return user ? <Dashboard /> : <Login />;
}
