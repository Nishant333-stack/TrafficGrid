import { createRoot } from "react-dom/client";
import "leaflet/dist/leaflet.css";
import "./styles.css";
import App from "./App.jsx";

// StrictMode intentionally omitted: its dev-only double-invoke of effects opens
// then immediately tears down the /ws/live WebSocket, which surfaces as the
// connection being dropped right after it opens.
createRoot(document.getElementById("root")).render(<App />);
