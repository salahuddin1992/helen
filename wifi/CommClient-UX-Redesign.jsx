import { useState, useCallback, useMemo } from "react";
import { MessageSquare, Phone, Video, Monitor, Users, Settings, Search, Plus, Send, PhoneOff, Mic, MicOff, VideoOff, Camera, ArrowLeft, Check, X, AlertTriangle, Shield, Globe, ChevronRight, User, Bell, Volume2, LogOut, Moon, Sun, Wifi, WifiOff, ScreenShare, ScreenShareOff, UserPlus, Hash, Lock, Smile, Paperclip, MoreVertical, Info, Star, Clock, CheckCheck, Eye, Trash2, Edit3, Copy, Download } from "lucide-react";

// ══════════════════════════════════════════════════════════════
// CommClient — Child-Friendly UX Redesign Spec
// Interactive Design System & Screen Prototypes
// ══════════════════════════════════════════════════════════════

const COLORS = {
  bg: "#0F172A", bgCard: "#1E293B", bgHover: "#334155", bgInput: "#0F172A",
  primary: "#3B82F6", primaryHover: "#2563EB", primaryLight: "#DBEAFE",
  success: "#22C55E", successBg: "#052E16", danger: "#EF4444", dangerBg: "#450A0A",
  warning: "#F59E0B", warningBg: "#451A03",
  text: "#F8FAFC", textMuted: "#94A3B8", textDim: "#64748B",
  border: "#334155", borderLight: "#475569",
  online: "#22C55E", offline: "#64748B", busy: "#EF4444",
  accent: "#8B5CF6",
};

// ── Shared UI primitives ─────────────────────────────────────

function BigButton({ icon: Icon, label, sublabel, onClick, color = COLORS.primary, hoverColor, size = "lg", disabled, badge, active, style }) {
  const sz = size === "xl" ? 72 : size === "lg" ? 56 : size === "md" ? 48 : 40;
  const iconSz = size === "xl" ? 32 : size === "lg" ? 24 : size === "md" ? 20 : 18;
  const fontSize = size === "xl" ? 18 : size === "lg" ? 15 : 13;
  return (
    <button onClick={onClick} disabled={disabled} style={{
      display: "flex", flexDirection: "column", alignItems: "center", gap: 6,
      background: "none", border: "none", cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.4 : 1, padding: 8, position: "relative", ...style
    }}>
      <div style={{
        width: sz, height: sz, borderRadius: sz / 2,
        background: active ? color : color + "22", border: `2px solid ${active ? color : color + "66"}`,
        display: "flex", alignItems: "center", justifyContent: "center",
        transition: "all 0.2s", boxShadow: active ? `0 0 20px ${color}44` : "none",
      }}>
        <Icon size={iconSz} color={active ? "#fff" : color} />
      </div>
      {label && <span style={{ fontSize, fontWeight: 600, color: active ? color : COLORS.text, textAlign: "center", lineHeight: 1.2 }}>{label}</span>}
      {sublabel && <span style={{ fontSize: 11, color: COLORS.textMuted, textAlign: "center" }}>{sublabel}</span>}
      {badge > 0 && <div style={{
        position: "absolute", top: 2, right: size === "xl" ? 8 : 2,
        background: COLORS.danger, color: "#fff", borderRadius: 10,
        minWidth: 20, height: 20, fontSize: 11, fontWeight: 700,
        display: "flex", alignItems: "center", justifyContent: "center", padding: "0 5px",
      }}>{badge}</div>}
    </button>
  );
}

function ActionBar({ children }) {
  return <div style={{ display: "flex", justifyContent: "center", gap: 16, padding: "16px 0", flexWrap: "wrap" }}>{children}</div>;
}

function Avatar({ name, size = 48, online, color }) {
  const c = color || ["#3B82F6","#8B5CF6","#EC4899","#F59E0B","#22C55E","#06B6D4"][Math.abs((name||"U").charCodeAt(0) * 7) % 6];
  return (
    <div style={{ position: "relative", flexShrink: 0 }}>
      <div style={{
        width: size, height: size, borderRadius: size / 2, background: c + "33",
        border: `2px solid ${c}`, display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: size * 0.4, fontWeight: 700, color: c,
      }}>{(name || "U")[0].toUpperCase()}</div>
      {online !== undefined && <div style={{
        position: "absolute", bottom: 1, right: 1,
        width: size * 0.26, height: size * 0.26, borderRadius: "50%",
        background: online ? COLORS.online : COLORS.offline,
        border: `2px solid ${COLORS.bgCard}`,
      }} />}
    </div>
  );
}

function Card({ children, onClick, active, style }) {
  return (
    <div onClick={onClick} style={{
      background: active ? COLORS.primary + "18" : COLORS.bgCard,
      border: `1px solid ${active ? COLORS.primary + "55" : COLORS.border}`,
      borderRadius: 16, padding: 16, cursor: onClick ? "pointer" : "default",
      transition: "all 0.2s", ...style,
    }}>{children}</div>
  );
}

function SectionTitle({ children, icon: Icon, action }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {Icon && <Icon size={18} color={COLORS.primary} />}
        <span style={{ fontSize: 14, fontWeight: 700, color: COLORS.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>{children}</span>
      </div>
      {action}
    </div>
  );
}

function Chip({ label, active, onClick, color = COLORS.primary }) {
  return (
    <button onClick={onClick} style={{
      padding: "8px 16px", borderRadius: 20, fontSize: 13, fontWeight: 600,
      background: active ? color : "transparent", color: active ? "#fff" : color,
      border: `2px solid ${active ? color : color + "44"}`, cursor: "pointer", transition: "all 0.2s",
    }}>{label}</button>
  );
}

function SpecNote({ children }) {
  return <div style={{ background: "#1E1B4B", border: "1px solid #4338CA55", borderRadius: 12, padding: 14, margin: "12px 0", fontSize: 13, color: "#A5B4FC", lineHeight: 1.6 }}>{children}</div>;
}

// ── SCREEN MOCKUPS ──────────────────────────────────────────

function WelcomeScreen({ onNext }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 24, padding: 32 }}>
      <div style={{ fontSize: 64 }}>💬</div>
      <h1 style={{ fontSize: 32, fontWeight: 800, color: COLORS.text, textAlign: "center", margin: 0 }}>CommClient</h1>
      <p style={{ fontSize: 18, color: COLORS.textMuted, textAlign: "center", maxWidth: 320, lineHeight: 1.6, margin: 0 }}>
        Talk with people on your network.<br/>No internet needed!
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, width: "100%", maxWidth: 300, marginTop: 16 }}>
        <button onClick={onNext} style={{
          padding: "18px 32px", borderRadius: 16, fontSize: 20, fontWeight: 700,
          background: COLORS.primary, color: "#fff", border: "none", cursor: "pointer",
          boxShadow: `0 4px 20px ${COLORS.primary}44`,
        }}>Get Started</button>
        <button style={{
          padding: "14px 32px", borderRadius: 16, fontSize: 16, fontWeight: 600,
          background: "transparent", color: COLORS.primary, border: `2px solid ${COLORS.primary}44`, cursor: "pointer",
        }}>I have an account</button>
      </div>
      <div style={{ display: "flex", gap: 16, marginTop: 12 }}>
        <Chip label="English" active />
        <Chip label="العربية" />
      </div>
    </div>
  );
}

function LoginScreen() {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 20, padding: 32 }}>
      <div style={{ fontSize: 48 }}>👋</div>
      <h2 style={{ fontSize: 26, fontWeight: 700, color: COLORS.text, margin: 0 }}>Welcome back!</h2>
      <p style={{ fontSize: 15, color: COLORS.textMuted, margin: 0 }}>Enter your name and password to continue</p>
      <div style={{ width: "100%", maxWidth: 340, display: "flex", flexDirection: "column", gap: 14, marginTop: 8 }}>
        <div>
          <label style={{ fontSize: 14, fontWeight: 600, color: COLORS.textMuted, marginBottom: 6, display: "block" }}>Your Name</label>
          <input placeholder="e.g. Ahmed" style={{
            width: "100%", padding: "16px 18px", borderRadius: 14, fontSize: 18,
            background: COLORS.bgInput, border: `2px solid ${COLORS.border}`, color: COLORS.text,
            outline: "none", boxSizing: "border-box",
          }} />
        </div>
        <div>
          <label style={{ fontSize: 14, fontWeight: 600, color: COLORS.textMuted, marginBottom: 6, display: "block" }}>Password</label>
          <input type="password" placeholder="Your secret password" style={{
            width: "100%", padding: "16px 18px", borderRadius: 14, fontSize: 18,
            background: COLORS.bgInput, border: `2px solid ${COLORS.border}`, color: COLORS.text,
            outline: "none", boxSizing: "border-box",
          }} />
        </div>
        <button style={{
          padding: "18px 32px", borderRadius: 14, fontSize: 18, fontWeight: 700,
          background: COLORS.primary, color: "#fff", border: "none", cursor: "pointer",
          marginTop: 8, boxShadow: `0 4px 20px ${COLORS.primary}44`,
        }}>Log In</button>
      </div>
      <SpecNote>
        <strong>UX Note:</strong> Server URL is auto-discovered via mDNS. No manual IP entry. If discovery fails, show a single "Server Address" field with helper text: "Ask your admin for the address".
      </SpecNote>
    </div>
  );
}

function RegisterScreen() {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 16, padding: 32 }}>
      <div style={{ fontSize: 48 }}>🎉</div>
      <h2 style={{ fontSize: 26, fontWeight: 700, color: COLORS.text, margin: 0 }}>Create Your Account</h2>
      <p style={{ fontSize: 15, color: COLORS.textMuted, margin: 0, textAlign: "center" }}>Choose a name that people will see</p>
      <div style={{ width: "100%", maxWidth: 340, display: "flex", flexDirection: "column", gap: 14, marginTop: 8 }}>
        <div>
          <label style={{ fontSize: 14, fontWeight: 600, color: COLORS.textMuted, marginBottom: 6, display: "block" }}>Pick a Name</label>
          <input placeholder="e.g. Ahmed" style={{
            width: "100%", padding: "16px 18px", borderRadius: 14, fontSize: 18,
            background: COLORS.bgInput, border: `2px solid ${COLORS.border}`, color: COLORS.text,
            outline: "none", boxSizing: "border-box",
          }} />
          <span style={{ fontSize: 12, color: COLORS.textDim, marginTop: 4, display: "block" }}>This is how others will find you</span>
        </div>
        <div>
          <label style={{ fontSize: 14, fontWeight: 600, color: COLORS.textMuted, marginBottom: 6, display: "block" }}>Display Name</label>
          <input placeholder="e.g. Ahmed Ali" style={{
            width: "100%", padding: "16px 18px", borderRadius: 14, fontSize: 18,
            background: COLORS.bgInput, border: `2px solid ${COLORS.border}`, color: COLORS.text,
            outline: "none", boxSizing: "border-box",
          }} />
        </div>
        <div>
          <label style={{ fontSize: 14, fontWeight: 600, color: COLORS.textMuted, marginBottom: 6, display: "block" }}>Create a Password</label>
          <input type="password" placeholder="At least 8 characters" style={{
            width: "100%", padding: "16px 18px", borderRadius: 14, fontSize: 18,
            background: COLORS.bgInput, border: `2px solid ${COLORS.border}`, color: COLORS.text,
            outline: "none", boxSizing: "border-box",
          }} />
          <div style={{ display: "flex", gap: 4, marginTop: 8 }}>
            {[1,2,3,4].map(i => <div key={i} style={{ flex: 1, height: 4, borderRadius: 2, background: i <= 2 ? COLORS.warning : COLORS.border }} />)}
          </div>
          <span style={{ fontSize: 12, color: COLORS.warning, marginTop: 4, display: "block" }}>Medium — add numbers or symbols to make it stronger</span>
        </div>
        <button style={{
          padding: "18px 32px", borderRadius: 14, fontSize: 18, fontWeight: 700,
          background: COLORS.primary, color: "#fff", border: "none", cursor: "pointer",
          marginTop: 8, boxShadow: `0 4px 20px ${COLORS.primary}44`,
        }}>Create Account</button>
      </div>
    </div>
  );
}

function HomeScreen({ onNav }) {
  const contacts = [
    { name: "Sarah", online: true, lastMsg: "See you tomorrow!", time: "2m", unread: 2 },
    { name: "Omar", online: true, lastMsg: "The file is ready", time: "15m", unread: 0 },
    { name: "Fatima", online: false, lastMsg: "Thanks!", time: "1h", unread: 0 },
    { name: "Ali", online: true, lastMsg: "Can we talk?", time: "3h", unread: 1 },
  ];
  const groups = [
    { name: "Team Chat", members: 5, lastMsg: "Meeting at 3pm", unread: 4 },
    { name: "Friends", members: 8, lastMsg: "Who's coming?", unread: 0 },
  ];
  return (
    <div style={{ display: "flex", height: "100%" }}>
      {/* Bottom Tab Bar (Mobile-style, always visible) */}
      <div style={{ display: "flex", flexDirection: "column", width: "100%" }}>
        {/* Header */}
        <div style={{ padding: "16px 20px", display: "flex", alignItems: "center", justifyContent: "space-between", borderBottom: `1px solid ${COLORS.border}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 28 }}>💬</span>
            <h1 style={{ fontSize: 22, fontWeight: 800, color: COLORS.text, margin: 0 }}>CommClient</h1>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 12px", background: COLORS.success + "22", borderRadius: 20, border: `1px solid ${COLORS.success}44` }}>
              <Wifi size={14} color={COLORS.success} />
              <span style={{ fontSize: 12, color: COLORS.success, fontWeight: 600 }}>Connected</span>
            </div>
            <button onClick={() => onNav("settings")} style={{ background: COLORS.bgCard, border: `1px solid ${COLORS.border}`, borderRadius: 12, padding: 8, cursor: "pointer" }}>
              <Settings size={20} color={COLORS.textMuted} />
            </button>
          </div>
        </div>

        {/* Quick Actions Row */}
        <div style={{ padding: "12px 20px", display: "flex", gap: 12 }}>
          <button onClick={() => onNav("new-chat")} style={{
            flex: 1, padding: "14px", borderRadius: 14, fontSize: 15, fontWeight: 600,
            background: COLORS.primary + "18", color: COLORS.primary, border: `2px solid ${COLORS.primary}33`,
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          }}><MessageSquare size={18} /> New Chat</button>
          <button onClick={() => onNav("new-group")} style={{
            flex: 1, padding: "14px", borderRadius: 14, fontSize: 15, fontWeight: 600,
            background: COLORS.accent + "18", color: COLORS.accent, border: `2px solid ${COLORS.accent}33`,
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          }}><Users size={18} /> New Group</button>
        </div>

        {/* Conversations */}
        <div style={{ flex: 1, overflowY: "auto", padding: "0 12px" }}>
          <SectionTitle icon={MessageSquare}>People</SectionTitle>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {contacts.map(c => (
              <div key={c.name} onClick={() => onNav("chat")} style={{
                display: "flex", alignItems: "center", gap: 14, padding: "14px 12px",
                borderRadius: 14, cursor: "pointer", background: c.unread ? COLORS.primary + "0A" : "transparent",
                border: c.unread ? `1px solid ${COLORS.primary}22` : "1px solid transparent",
              }}>
                <Avatar name={c.name} size={50} online={c.online} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={{ fontSize: 16, fontWeight: c.unread ? 700 : 500, color: COLORS.text }}>{c.name}</span>
                    <span style={{ fontSize: 12, color: c.unread ? COLORS.primary : COLORS.textDim }}>{c.time}</span>
                  </div>
                  <p style={{ fontSize: 14, color: c.unread ? COLORS.text : COLORS.textMuted, margin: "4px 0 0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.lastMsg}</p>
                </div>
                {c.unread > 0 && <div style={{
                  background: COLORS.primary, color: "#fff", borderRadius: 12,
                  minWidth: 24, height: 24, fontSize: 12, fontWeight: 700,
                  display: "flex", alignItems: "center", justifyContent: "center", padding: "0 6px",
                }}>{c.unread}</div>}
              </div>
            ))}
          </div>

          <SectionTitle icon={Users}>Groups</SectionTitle>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {groups.map(g => (
              <div key={g.name} onClick={() => onNav("group-chat")} style={{
                display: "flex", alignItems: "center", gap: 14, padding: "14px 12px",
                borderRadius: 14, cursor: "pointer",
                background: g.unread ? COLORS.accent + "0A" : "transparent",
                border: g.unread ? `1px solid ${COLORS.accent}22` : "1px solid transparent",
              }}>
                <div style={{
                  width: 50, height: 50, borderRadius: 14,
                  background: COLORS.accent + "22", border: `2px solid ${COLORS.accent}44`,
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}>
                  <Users size={24} color={COLORS.accent} />
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ fontSize: 16, fontWeight: g.unread ? 700 : 500, color: COLORS.text }}>{g.name}</span>
                    <span style={{ fontSize: 12, color: COLORS.textDim }}>{g.members} people</span>
                  </div>
                  <p style={{ fontSize: 14, color: COLORS.textMuted, margin: "4px 0 0" }}>{g.lastMsg}</p>
                </div>
                {g.unread > 0 && <div style={{
                  background: COLORS.accent, color: "#fff", borderRadius: 12,
                  minWidth: 24, height: 24, fontSize: 12, fontWeight: 700,
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}>{g.unread}</div>}
              </div>
            ))}
          </div>
        </div>

        {/* Bottom Navigation Bar */}
        <div style={{
          display: "flex", borderTop: `1px solid ${COLORS.border}`,
          background: COLORS.bg, padding: "8px 0 12px",
        }}>
          {[
            { icon: MessageSquare, label: "Chats", active: true, badge: 3 },
            { icon: Users, label: "People" },
            { icon: Phone, label: "Calls" },
            { icon: Settings, label: "Settings" },
          ].map(tab => (
            <button key={tab.label} style={{
              flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 4,
              background: "none", border: "none", cursor: "pointer", padding: "6px 0", position: "relative",
            }}>
              <tab.icon size={24} color={tab.active ? COLORS.primary : COLORS.textDim} />
              <span style={{ fontSize: 11, fontWeight: tab.active ? 700 : 500, color: tab.active ? COLORS.primary : COLORS.textDim }}>{tab.label}</span>
              {tab.badge > 0 && <div style={{
                position: "absolute", top: 0, right: "calc(50% - 18px)",
                background: COLORS.danger, color: "#fff", borderRadius: 8,
                minWidth: 16, height: 16, fontSize: 10, fontWeight: 700,
                display: "flex", alignItems: "center", justifyContent: "center",
              }}>{tab.badge}</div>}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function ChatScreen({ onBack }) {
  const msgs = [
    { from: "Sarah", text: "Hey! Are you coming to the meeting?", time: "10:30 AM", mine: false },
    { from: "Me", text: "Yes, I'll be there in 5 minutes", time: "10:31 AM", mine: true, status: "read" },
    { from: "Sarah", text: "Great! See you soon 😊", time: "10:32 AM", mine: false },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Chat Header — big, clear */}
      <div style={{ padding: "12px 16px", display: "flex", alignItems: "center", gap: 12, borderBottom: `1px solid ${COLORS.border}`, background: COLORS.bgCard }}>
        <button onClick={onBack} style={{ background: "none", border: "none", cursor: "pointer", padding: 8 }}>
          <ArrowLeft size={24} color={COLORS.text} />
        </button>
        <Avatar name="Sarah" size={44} online={true} />
        <div style={{ flex: 1 }}>
          <span style={{ fontSize: 18, fontWeight: 700, color: COLORS.text }}>Sarah</span>
          <div style={{ fontSize: 13, color: COLORS.success }}>Online now</div>
        </div>
        {/* Big call buttons right in the header */}
        <button style={{
          width: 48, height: 48, borderRadius: 24,
          background: COLORS.success + "22", border: `2px solid ${COLORS.success}44`,
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}><Phone size={22} color={COLORS.success} /></button>
        <button style={{
          width: 48, height: 48, borderRadius: 24,
          background: COLORS.primary + "22", border: `2px solid ${COLORS.primary}44`,
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}><Video size={22} color={COLORS.primary} /></button>
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: "auto", padding: "16px 20px", display: "flex", flexDirection: "column", gap: 8 }}>
        {msgs.map((m, i) => (
          <div key={i} style={{ display: "flex", justifyContent: m.mine ? "flex-end" : "flex-start" }}>
            <div style={{
              maxWidth: "75%", padding: "12px 16px", borderRadius: 18,
              background: m.mine ? COLORS.primary : COLORS.bgCard,
              borderBottomRightRadius: m.mine ? 4 : 18,
              borderBottomLeftRadius: m.mine ? 18 : 4,
            }}>
              <p style={{ fontSize: 16, color: "#fff", margin: 0, lineHeight: 1.5 }}>{m.text}</p>
              <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 4, marginTop: 4 }}>
                <span style={{ fontSize: 11, color: m.mine ? "#ffffff88" : COLORS.textDim }}>{m.time}</span>
                {m.mine && <CheckCheck size={14} color={m.status === "read" ? "#60A5FA" : "#ffffff55"} />}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Message Input — large, obvious */}
      <div style={{ padding: "12px 16px", borderTop: `1px solid ${COLORS.border}`, background: COLORS.bgCard }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 8,
          background: COLORS.bgInput, borderRadius: 28, padding: "6px 8px 6px 18px",
          border: `2px solid ${COLORS.border}`,
        }}>
          <button style={{ background: "none", border: "none", cursor: "pointer", padding: 8 }}>
            <Paperclip size={22} color={COLORS.textMuted} />
          </button>
          <input placeholder="Type a message..." style={{
            flex: 1, background: "none", border: "none", outline: "none",
            fontSize: 17, color: COLORS.text, padding: "10px 0",
          }} />
          <button style={{
            width: 48, height: 48, borderRadius: 24,
            background: COLORS.primary, border: "none", cursor: "pointer",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}><Send size={22} color="#fff" /></button>
        </div>
        <SpecNote>
          <strong>Spec:</strong> Send button is always visible (never hidden). Attach button opens a simple file picker. Emoji picker via long-press on attach or separate smiley button. No markdown formatting — plain text only.
        </SpecNote>
      </div>
    </div>
  );
}

function IncomingCallScreen() {
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
      height: "100%", background: `linear-gradient(180deg, ${COLORS.bg} 0%, #0C1222 100%)`, gap: 32, padding: 32,
    }}>
      <div style={{ fontSize: 20, color: COLORS.textMuted, fontWeight: 500 }}>Incoming Call...</div>
      <Avatar name="Omar" size={120} />
      <h2 style={{ fontSize: 32, fontWeight: 700, color: COLORS.text, margin: 0 }}>Omar</h2>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Phone size={20} color={COLORS.primary} />
        <span style={{ fontSize: 18, color: COLORS.primary }}>Voice Call</span>
      </div>
      <div style={{ display: "flex", gap: 48, marginTop: 32 }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
          <button style={{
            width: 80, height: 80, borderRadius: 40, background: COLORS.danger,
            border: "none", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: `0 0 30px ${COLORS.danger}44`,
          }}><PhoneOff size={36} color="#fff" /></button>
          <span style={{ fontSize: 16, fontWeight: 600, color: COLORS.danger }}>Decline</span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
          <button style={{
            width: 80, height: 80, borderRadius: 40, background: COLORS.success,
            border: "none", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: `0 0 30px ${COLORS.success}44`, animation: "pulse 2s infinite",
          }}><Phone size={36} color="#fff" /></button>
          <span style={{ fontSize: 16, fontWeight: 600, color: COLORS.success }}>Answer</span>
        </div>
      </div>
      <SpecNote>
        <strong>Spec:</strong> Buttons are 80px diameter — impossible to miss. Decline on left (red), Answer on right (green, pulsing). Full-screen overlay. Sound + vibration pattern. Auto-timeout after 60 seconds with "Missed Call" notification.
      </SpecNote>
    </div>
  );
}

function ActiveCallScreen() {
  const [muted, setMuted] = useState(false);
  const [videoOn, setVideoOn] = useState(false);
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "space-between",
      height: "100%", background: COLORS.bg, padding: 24,
    }}>
      <div style={{ textAlign: "center", marginTop: 32 }}>
        <Avatar name="Omar" size={100} />
        <h2 style={{ fontSize: 28, fontWeight: 700, color: COLORS.text, margin: "16px 0 4px" }}>Omar</h2>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
          <div style={{ width: 8, height: 8, borderRadius: 4, background: COLORS.success, animation: "pulse 1.5s infinite" }} />
          <span style={{ fontSize: 16, color: COLORS.success }}>3:42</span>
        </div>
      </div>

      {/* Control Buttons — big and spaced */}
      <div style={{ display: "flex", gap: 20, marginBottom: 48 }}>
        <button onClick={() => setMuted(!muted)} style={{
          width: 64, height: 64, borderRadius: 32,
          background: muted ? COLORS.danger + "22" : COLORS.bgCard,
          border: `2px solid ${muted ? COLORS.danger : COLORS.border}`,
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}>{muted ? <MicOff size={28} color={COLORS.danger} /> : <Mic size={28} color={COLORS.text} />}</button>

        <button onClick={() => setVideoOn(!videoOn)} style={{
          width: 64, height: 64, borderRadius: 32,
          background: videoOn ? COLORS.primary + "22" : COLORS.bgCard,
          border: `2px solid ${videoOn ? COLORS.primary : COLORS.border}`,
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}>{videoOn ? <Camera size={28} color={COLORS.primary} /> : <VideoOff size={28} color={COLORS.textMuted} />}</button>

        <button style={{
          width: 64, height: 64, borderRadius: 32,
          background: COLORS.bgCard, border: `2px solid ${COLORS.border}`,
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}><Monitor size={28} color={COLORS.text} /></button>

        <button style={{
          width: 72, height: 72, borderRadius: 36,
          background: COLORS.danger, border: "none",
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
          boxShadow: `0 0 24px ${COLORS.danger}44`,
        }}><PhoneOff size={32} color="#fff" /></button>
      </div>

      <SpecNote>
        <strong>Spec:</strong> 4 buttons only: Mute (toggle), Camera (toggle), Share Screen, End Call (red, slightly larger). Labels appear below on hover. End Call is always red and bigger. Screen share replaces camera feed with screen preview.
      </SpecNote>
    </div>
  );
}

function GroupCallScreen() {
  const participants = ["Me", "Sarah", "Omar", "Fatima", "Ali"];
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: COLORS.bg }}>
      <div style={{ padding: "12px 20px", display: "flex", alignItems: "center", justifyContent: "space-between", borderBottom: `1px solid ${COLORS.border}` }}>
        <h3 style={{ fontSize: 18, fontWeight: 700, color: COLORS.text, margin: 0 }}>Team Chat — Group Call</h3>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <Users size={16} color={COLORS.primary} />
          <span style={{ fontSize: 14, color: COLORS.primary, fontWeight: 600 }}>{participants.length} people</span>
        </div>
      </div>

      <div style={{ flex: 1, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12, padding: 16, alignContent: "center" }}>
        {participants.map(p => (
          <div key={p} style={{
            background: COLORS.bgCard, borderRadius: 16, padding: 20,
            display: "flex", flexDirection: "column", alignItems: "center", gap: 10,
            border: p === "Me" ? `2px solid ${COLORS.primary}44` : `1px solid ${COLORS.border}`,
          }}>
            <Avatar name={p} size={60} />
            <span style={{ fontSize: 15, fontWeight: 600, color: COLORS.text }}>{p === "Me" ? "You" : p}</span>
            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <Mic size={14} color={COLORS.success} />
              <span style={{ fontSize: 12, color: COLORS.success }}>Speaking</span>
            </div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", justifyContent: "center", gap: 20, padding: "16px 0 24px", borderTop: `1px solid ${COLORS.border}` }}>
        <button style={{
          width: 56, height: 56, borderRadius: 28, background: COLORS.bgCard, border: `2px solid ${COLORS.border}`,
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}><Mic size={24} color={COLORS.text} /></button>
        <button style={{
          width: 56, height: 56, borderRadius: 28, background: COLORS.bgCard, border: `2px solid ${COLORS.border}`,
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}><Camera size={24} color={COLORS.text} /></button>
        <button style={{
          width: 56, height: 56, borderRadius: 28, background: COLORS.bgCard, border: `2px solid ${COLORS.border}`,
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}><Monitor size={24} color={COLORS.text} /></button>
        <button style={{
          width: 64, height: 64, borderRadius: 32, background: COLORS.danger, border: "none",
          display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
        }}><PhoneOff size={28} color="#fff" /></button>
      </div>
    </div>
  );
}

function ErrorMessages() {
  const errors = [
    { old: "WebSocket connection failed: ECONNREFUSED", new: "Can't reach the server. Make sure you're on the same WiFi network.", icon: "🔌", color: COLORS.danger },
    { old: "Authentication token expired", new: "Your session ended. Please log in again.", icon: "🔒", color: COLORS.warning },
    { old: "Rate limit exceeded: 429", new: "You're sending too fast! Wait a moment and try again.", icon: "⏳", color: COLORS.warning },
    { old: "ONAVAILABLE: getUserMedia error", new: "We can't access your microphone. Check that it's plugged in and allowed.", icon: "🎤", color: COLORS.danger },
    { old: "ICE connection failed", new: "The call couldn't connect. Check your network and try calling again.", icon: "📞", color: COLORS.danger },
    { old: "File too large: exceeds MAX_UPLOAD_SIZE_MB", new: "This file is too big. Try a file smaller than 100 MB.", icon: "📁", color: COLORS.warning },
    { old: "Channel not found: 404", new: "This conversation was removed. Go back and start a new one.", icon: "💬", color: COLORS.warning },
    { old: "Account locked: too many failed attempts", new: "Too many wrong passwords. Wait 5 minutes, then try again.", icon: "🚫", color: COLORS.danger },
    { old: "Database locked: OperationalError", new: "Something went wrong saving your data. Please try again.", icon: "💾", color: COLORS.danger },
    { old: "Socket reconnection failed after max retries", new: "Lost connection to the server. Restart the app to reconnect.", icon: "📡", color: COLORS.danger },
  ];
  return (
    <div style={{ padding: 20, overflowY: "auto", height: "100%" }}>
      <h2 style={{ fontSize: 22, fontWeight: 700, color: COLORS.text, margin: "0 0 8px" }}>Error Messages — Before & After</h2>
      <p style={{ fontSize: 14, color: COLORS.textMuted, margin: "0 0 16px" }}>Every technical error rewritten in plain human language</p>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {errors.map((e, i) => (
          <Card key={i} style={{ padding: 14 }}>
            <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
              <span style={{ fontSize: 28, flexShrink: 0 }}>{e.icon}</span>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, color: COLORS.danger + "aa", fontFamily: "monospace", background: COLORS.dangerBg, padding: "4px 8px", borderRadius: 6, marginBottom: 8, textDecoration: "line-through" }}>{e.old}</div>
                <div style={{ fontSize: 15, color: COLORS.text, fontWeight: 600, lineHeight: 1.4 }}>{e.new}</div>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

function ConfirmationDialogs() {
  return (
    <div style={{ padding: 20, overflowY: "auto", height: "100%" }}>
      <h2 style={{ fontSize: 22, fontWeight: 700, color: COLORS.text, margin: "0 0 8px" }}>Confirmation Dialogs & Safe Actions</h2>
      <p style={{ fontSize: 14, color: COLORS.textMuted, margin: "0 0 20px" }}>Every destructive or important action requires clear confirmation</p>

      {/* Leave Group */}
      <Card style={{ marginBottom: 16, textAlign: "center", padding: 24 }}>
        <div style={{ fontSize: 48, marginBottom: 12 }}>👋</div>
        <h3 style={{ fontSize: 20, fontWeight: 700, color: COLORS.text, margin: "0 0 8px" }}>Leave this group?</h3>
        <p style={{ fontSize: 15, color: COLORS.textMuted, margin: "0 0 20px" }}>You won't get messages from "Team Chat" anymore. You can rejoin later if someone invites you.</p>
        <div style={{ display: "flex", gap: 12, justifyContent: "center" }}>
          <button style={{ padding: "14px 32px", borderRadius: 14, fontSize: 16, fontWeight: 600, background: COLORS.bgCard, color: COLORS.text, border: `2px solid ${COLORS.border}`, cursor: "pointer" }}>Stay</button>
          <button style={{ padding: "14px 32px", borderRadius: 14, fontSize: 16, fontWeight: 600, background: COLORS.danger, color: "#fff", border: "none", cursor: "pointer" }}>Leave Group</button>
        </div>
      </Card>

      {/* Delete Message */}
      <Card style={{ marginBottom: 16, textAlign: "center", padding: 24 }}>
        <div style={{ fontSize: 48, marginBottom: 12 }}>🗑️</div>
        <h3 style={{ fontSize: 20, fontWeight: 700, color: COLORS.text, margin: "0 0 8px" }}>Delete this message?</h3>
        <p style={{ fontSize: 15, color: COLORS.textMuted, margin: "0 0 20px" }}>This can't be undone. Everyone will see it was removed.</p>
        <div style={{ display: "flex", gap: 12, justifyContent: "center" }}>
          <button style={{ padding: "14px 32px", borderRadius: 14, fontSize: 16, fontWeight: 600, background: COLORS.bgCard, color: COLORS.text, border: `2px solid ${COLORS.border}`, cursor: "pointer" }}>Keep It</button>
          <button style={{ padding: "14px 32px", borderRadius: 14, fontSize: 16, fontWeight: 600, background: COLORS.danger, color: "#fff", border: "none", cursor: "pointer" }}>Delete</button>
        </div>
      </Card>

      {/* End Call */}
      <Card style={{ marginBottom: 16, textAlign: "center", padding: 24 }}>
        <div style={{ fontSize: 48, marginBottom: 12 }}>📞</div>
        <h3 style={{ fontSize: 20, fontWeight: 700, color: COLORS.text, margin: "0 0 8px" }}>End this call?</h3>
        <p style={{ fontSize: 15, color: COLORS.textMuted, margin: "0 0 16px" }}>You'll be disconnected from the group call. Others will continue without you.</p>
        <div style={{ display: "flex", gap: 12, justifyContent: "center" }}>
          <button style={{ padding: "14px 32px", borderRadius: 14, fontSize: 16, fontWeight: 600, background: COLORS.bgCard, color: COLORS.text, border: `2px solid ${COLORS.border}`, cursor: "pointer" }}>Stay</button>
          <button style={{ padding: "14px 32px", borderRadius: 14, fontSize: 16, fontWeight: 600, background: COLORS.danger, color: "#fff", border: "none", cursor: "pointer" }}>Leave Call</button>
        </div>
      </Card>

      {/* Log Out */}
      <Card style={{ marginBottom: 16, textAlign: "center", padding: 24 }}>
        <div style={{ fontSize: 48, marginBottom: 12 }}>🚪</div>
        <h3 style={{ fontSize: 20, fontWeight: 700, color: COLORS.text, margin: "0 0 8px" }}>Log out?</h3>
        <p style={{ fontSize: 15, color: COLORS.textMuted, margin: "0 0 16px" }}>Your messages are saved. You'll need your password to log back in.</p>
        <div style={{ display: "flex", gap: 12, justifyContent: "center" }}>
          <button style={{ padding: "14px 32px", borderRadius: 14, fontSize: 16, fontWeight: 600, background: COLORS.bgCard, color: COLORS.text, border: `2px solid ${COLORS.border}`, cursor: "pointer" }}>Cancel</button>
          <button style={{ padding: "14px 32px", borderRadius: 14, fontSize: 16, fontWeight: 600, background: COLORS.warning, color: "#fff", border: "none", cursor: "pointer" }}>Log Out</button>
        </div>
      </Card>

      <SpecNote>
        <strong>Design Rules:</strong> Safe option is always on the LEFT. Destructive action is always on the RIGHT, in red. All dialogs use simple language + an emoji icon. No "Are you sure?" — instead explain what will happen. "Cancel" is never red.
      </SpecNote>
    </div>
  );
}

function SettingsScreen() {
  return (
    <div style={{ padding: 20, overflowY: "auto", height: "100%" }}>
      <h2 style={{ fontSize: 22, fontWeight: 700, color: COLORS.text, margin: "0 0 20px" }}>Settings</h2>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {[
          { icon: User, label: "My Profile", sub: "Change your name or picture", color: COLORS.primary },
          { icon: Bell, label: "Notifications", sub: "Sound and alerts", color: COLORS.warning },
          { icon: Volume2, label: "Sound & Video", sub: "Microphone, speaker, camera", color: COLORS.success },
          { icon: Globe, label: "Language", sub: "English / العربية", color: COLORS.accent },
          { icon: Moon, label: "Appearance", sub: "Light or dark theme", color: "#EC4899" },
        ].map(item => (
          <div key={item.label} style={{
            display: "flex", alignItems: "center", gap: 14, padding: "16px",
            background: COLORS.bgCard, borderRadius: 14, cursor: "pointer",
            border: `1px solid ${COLORS.border}`,
          }}>
            <div style={{
              width: 44, height: 44, borderRadius: 12,
              background: item.color + "22", border: `1px solid ${item.color}33`,
              display: "flex", alignItems: "center", justifyContent: "center",
            }}><item.icon size={22} color={item.color} /></div>
            <div style={{ flex: 1 }}>
              <span style={{ fontSize: 16, fontWeight: 600, color: COLORS.text }}>{item.label}</span>
              <div style={{ fontSize: 13, color: COLORS.textMuted }}>{item.sub}</div>
            </div>
            <ChevronRight size={20} color={COLORS.textDim} />
          </div>
        ))}

        <div style={{ marginTop: 16, paddingTop: 16, borderTop: `1px solid ${COLORS.border}` }}>
          <div style={{
            display: "flex", alignItems: "center", gap: 14, padding: "16px",
            background: COLORS.dangerBg, borderRadius: 14, cursor: "pointer",
            border: `1px solid ${COLORS.danger}33`,
          }}>
            <div style={{
              width: 44, height: 44, borderRadius: 12,
              background: COLORS.danger + "22",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}><LogOut size={22} color={COLORS.danger} /></div>
            <span style={{ fontSize: 16, fontWeight: 600, color: COLORS.danger }}>Log Out</span>
          </div>
        </div>
      </div>

      <SpecNote>
        <strong>Spec:</strong> Settings are flat — one level deep. No nested menus. Each setting opens an inline panel, not a new page. Audio/Video settings include a live preview and test button. No "Server URL", "JWT", "WebSocket" or any technical jargon exposed.
      </SpecNote>
    </div>
  );
}

function ArchitectureSpec() {
  return (
    <div style={{ padding: 20, overflowY: "auto", height: "100%" }}>
      <h2 style={{ fontSize: 22, fontWeight: 700, color: COLORS.text, margin: "0 0 4px" }}>1. Simplified UX Architecture</h2>
      <p style={{ fontSize: 14, color: COLORS.textMuted, marginBottom: 16 }}>Reduction from 6 navigation items + complex sub-routes to a 4-tab flat structure</p>

      <Card style={{ marginBottom: 16, padding: 20 }}>
        <h3 style={{ fontSize: 16, fontWeight: 700, color: COLORS.primary, margin: "0 0 12px" }}>Screen Flow Hierarchy</h3>
        <div style={{ fontFamily: "monospace", fontSize: 13, color: COLORS.text, lineHeight: 2, background: COLORS.bg, borderRadius: 12, padding: 16 }}>
          <div>Welcome Screen (first launch only)</div>
          <div style={{ paddingLeft: 20 }}>├─ Login (auto-discover server)</div>
          <div style={{ paddingLeft: 20 }}>└─ Register (3 fields only)</div>
          <div style={{ marginTop: 8 }}>Main App (4 bottom tabs)</div>
          <div style={{ paddingLeft: 20 }}>├─ <span style={{ color: COLORS.primary }}>💬 Chats</span> (default home)</div>
          <div style={{ paddingLeft: 40 }}>├─ Chat Detail → call/video/share buttons in header</div>
          <div style={{ paddingLeft: 40 }}>├─ New Chat → pick a person from list</div>
          <div style={{ paddingLeft: 40 }}>└─ New Group → name + pick people</div>
          <div style={{ paddingLeft: 20 }}>├─ <span style={{ color: COLORS.accent }}>👥 People</span> (online contacts)</div>
          <div style={{ paddingLeft: 40 }}>└─ Tap person → quick actions (chat/call/video)</div>
          <div style={{ paddingLeft: 20 }}>├─ <span style={{ color: COLORS.success }}>📞 Calls</span> (call history)</div>
          <div style={{ paddingLeft: 40 }}>└─ Tap entry → redial</div>
          <div style={{ paddingLeft: 20 }}>└─ <span style={{ color: COLORS.textMuted }}>⚙️ Settings</span> (5 items, flat)</div>
          <div style={{ marginTop: 8 }}>Overlay Screens (modal, full-screen)</div>
          <div style={{ paddingLeft: 20 }}>├─ Incoming Call (accept/decline)</div>
          <div style={{ paddingLeft: 20 }}>├─ Active Call (controls bar)</div>
          <div style={{ paddingLeft: 20 }}>└─ Screen Share (preview + stop)</div>
        </div>
      </Card>

      <Card style={{ marginBottom: 16, padding: 20 }}>
        <h3 style={{ fontSize: 16, fontWeight: 700, color: COLORS.primary, margin: "0 0 12px" }}>Key UX Reductions (Before → After)</h3>
        <table style={{ width: "100%", fontSize: 13, color: COLORS.text, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${COLORS.border}` }}>
              <th style={{ textAlign: "left", padding: "8px 0", color: COLORS.textMuted }}>Area</th>
              <th style={{ textAlign: "left", padding: "8px 0", color: COLORS.danger }}>Before</th>
              <th style={{ textAlign: "left", padding: "8px 0", color: COLORS.success }}>After</th>
            </tr>
          </thead>
          <tbody>
            {[
              ["Navigation", "6 sidebar icons (icon-only, 16px wide)", "4 bottom tabs with labels"],
              ["Start a Chat", "Click icon → search → click user → type", "Tap 'New Chat' → tap person → type"],
              ["Make a Call", "Navigate to contacts → hover → find call icon", "Open chat → tap big phone button in header"],
              ["Screen Share", "During call → find share button in controls", "During call → tap screen icon (4 buttons only)"],
              ["Create Group", "Navigate to Groups page → fill form", "Tap 'New Group' button on home → name + pick"],
              ["Settings", "6 sub-sections with device IDs", "5 plain items, one level, no jargon"],
              ["Server Setup", "Manual IP:port entry", "Auto-discovery via mDNS, fallback: 1 field"],
              ["Login", "Server URL + username + password", "Name + password (server auto-detected)"],
              ["Error Messages", "Technical HTTP/WebSocket codes", "Plain language with recovery action"],
            ].map(([area, before, after]) => (
              <tr key={area} style={{ borderBottom: `1px solid ${COLORS.border}22` }}>
                <td style={{ padding: "10px 8px 10px 0", fontWeight: 600 }}>{area}</td>
                <td style={{ padding: "10px 8px", color: COLORS.textMuted }}>{before}</td>
                <td style={{ padding: "10px 8px", color: COLORS.success }}>{after}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card style={{ padding: 20 }}>
        <h3 style={{ fontSize: 16, fontWeight: 700, color: COLORS.primary, margin: "0 0 12px" }}>Design Principles Enforced</h3>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {[
            ["1-Tap Rule", "Every core action reachable in 1-2 taps from home"],
            ["No Dead Ends", "Every screen has a clear back button and cancel path"],
            ["Visible State", "Connection status always shown (green = connected)"],
            ["Forgiving", "Undo where possible, confirm before destructive actions"],
            ["No Jargon", "Zero technical terms in user-facing text"],
            ["Minimum Inputs", "Registration: 3 fields. Login: 2 fields. New group: 2 steps."],
            ["Big Targets", "All interactive elements ≥ 48px (WCAG AAA touch target)"],
            ["Consistent Layout", "Back arrow always top-left. Primary action always bottom-right."],
          ].map(([title, desc]) => (
            <div key={title} style={{ display: "flex", gap: 8 }}>
              <Check size={16} color={COLORS.success} style={{ flexShrink: 0, marginTop: 2 }} />
              <span style={{ fontSize: 14, color: COLORS.text }}><strong>{title}:</strong> {desc}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

function ComponentSpec() {
  return (
    <div style={{ padding: 20, overflowY: "auto", height: "100%" }}>
      <h2 style={{ fontSize: 22, fontWeight: 700, color: COLORS.text, margin: "0 0 4px" }}>7-8. Component Recommendations & Implementation Plan</h2>
      <p style={{ fontSize: 14, color: COLORS.textMuted, marginBottom: 16 }}>Implementation-ready component system for the redesigned UI</p>

      <Card style={{ marginBottom: 16, padding: 20 }}>
        <h3 style={{ fontSize: 16, fontWeight: 700, color: COLORS.primary, margin: "0 0 12px" }}>Component Library (New / Modified)</h3>
        <table style={{ width: "100%", fontSize: 12, color: COLORS.text, borderCollapse: "collapse" }}>
          <thead><tr style={{ borderBottom: `2px solid ${COLORS.primary}44` }}>
            <th style={{ textAlign: "left", padding: 8, color: COLORS.primary }}>Component</th>
            <th style={{ textAlign: "left", padding: 8, color: COLORS.primary }}>Props</th>
            <th style={{ textAlign: "left", padding: 8, color: COLORS.primary }}>Behavior</th>
          </tr></thead>
          <tbody>
            {[
              ["BigButton", "icon, label, size(sm|md|lg|xl), color, badge, active, disabled, onClick", "Circular icon + label below. Minimum 48px target. Badge for notifications."],
              ["Avatar", "name, size, online?, color?", "First-letter fallback. Green/gray dot for presence. 6-color auto-palette."],
              ["BottomTabBar", "tabs[], activeTab, onChange", "4 tabs max. Icon + label. Badge count. Fixed at bottom. 60px height."],
              ["ChatBubble", "text, time, mine, status(sent|delivered|read)", "Blue (mine) or gray (theirs). Rounded corners. Check marks for status."],
              ["MessageInput", "onSend, onAttach, placeholder", "Pill-shaped. Attach left, text center, Send right (always visible). 56px height."],
              ["ContactRow", "name, online, lastMsg, time, unread, onClick", "Avatar + name + preview + time + badge. 72px row height. Tap opens chat."],
              ["CallOverlay", "type(incoming|active|group), participants[], onAccept, onDecline, onEnd", "Full-screen modal. 80px answer/decline buttons. Auto-timeout 60s."],
              ["CallControls", "muted, videoOn, sharing, onToggle*", "4 buttons only: Mic, Camera, Screen, End. End is bigger + red."],
              ["ConfirmDialog", "emoji, title, message, confirmLabel, cancelLabel, danger?, onConfirm, onCancel", "Centered card. Emoji top. Explain what happens. Safe left, destructive right."],
              ["ErrorToast", "emoji, message, action?, onAction", "Non-blocking. Auto-dismiss 5s. Tap for retry if action provided. No error codes."],
              ["ConnectionBadge", "status(connected|reconnecting|offline)", "Green pill / yellow spinner / red X. Always in header. 28px height."],
              ["SearchBar", "placeholder, value, onChange", "Rounded input. Search icon left. Clear X right. 48px height."],
              ["SectionHeader", "icon, label, action?", "Uppercase muted label. Optional right-side action button."],
              ["QuickActionBar", "actions[]", "Horizontal row of large pill buttons. Used on home screen."],
            ].map(([name, props, behavior]) => (
              <tr key={name} style={{ borderBottom: `1px solid ${COLORS.border}22` }}>
                <td style={{ padding: 8, fontWeight: 700, fontFamily: "monospace", color: COLORS.primary, whiteSpace: "nowrap" }}>{name}</td>
                <td style={{ padding: 8, fontFamily: "monospace", fontSize: 11, color: COLORS.textMuted }}>{props}</td>
                <td style={{ padding: 8, color: COLORS.text }}>{behavior}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card style={{ marginBottom: 16, padding: 20 }}>
        <h3 style={{ fontSize: 16, fontWeight: 700, color: COLORS.primary, margin: "0 0 12px" }}>Implementation Phases</h3>
        {[
          { phase: "Phase 1: Navigation Overhaul", items: ["Replace Sidebar (16px icon-only) with BottomTabBar (4 tabs, labeled)", "Merge Groups into Chats tab (unified conversation list)", "Move call initiation into ChatHeader (phone + video buttons)", "Remove standalone Groups page"], color: COLORS.primary },
          { phase: "Phase 2: Component Replacement", items: ["Replace all inline Tailwind button patterns with BigButton component", "Replace ContactList hover-reveal actions with always-visible ActionRow", "Replace Modal with ConfirmDialog for all destructive actions", "Add ConnectionBadge to MainLayout header (always visible)"], color: COLORS.accent },
          { phase: "Phase 3: Simplification", items: ["Replace SettingsView 6-section layout with flat 5-item list", "Hide Server URL behind auto-discovery (mDNS)", "Remove all technical labels from UI (JWT, WebSocket, ICE, SDP...)", "Replace all error messages via ErrorToast with rewritten copy"], color: COLORS.success },
          { phase: "Phase 4: Polish & Safety", items: ["Add ConfirmDialog to: leave group, delete message, end call, logout", "Add password strength meter to RegisterForm", "Add onboarding Welcome screen (first launch)", "Full AR/EN i18n pass on all new labels"], color: COLORS.warning },
        ].map(p => (
          <div key={p.phase} style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
              <div style={{ width: 8, height: 8, borderRadius: 4, background: p.color }} />
              <span style={{ fontSize: 14, fontWeight: 700, color: p.color }}>{p.phase}</span>
            </div>
            {p.items.map(item => (
              <div key={item} style={{ display: "flex", gap: 8, marginLeft: 16, marginBottom: 4 }}>
                <span style={{ color: COLORS.textDim }}>→</span>
                <span style={{ fontSize: 13, color: COLORS.text }}>{item}</span>
              </div>
            ))}
          </div>
        ))}
      </Card>

      <Card style={{ padding: 20 }}>
        <h3 style={{ fontSize: 16, fontWeight: 700, color: COLORS.primary, margin: "0 0 12px" }}>i18n Label Map (EN / AR) — Core Buttons & States</h3>
        <table style={{ width: "100%", fontSize: 12, color: COLORS.text, borderCollapse: "collapse" }}>
          <thead><tr style={{ borderBottom: `2px solid ${COLORS.primary}44` }}>
            <th style={{ textAlign: "left", padding: 8, color: COLORS.primary }}>Key</th>
            <th style={{ textAlign: "left", padding: 8, color: COLORS.primary }}>English</th>
            <th style={{ textAlign: "right", padding: 8, color: COLORS.primary, direction: "rtl" }}>العربية</th>
          </tr></thead>
          <tbody>
            {[
              ["nav.chats", "Chats", "المحادثات"],
              ["nav.people", "People", "الأشخاص"],
              ["nav.calls", "Calls", "المكالمات"],
              ["nav.settings", "Settings", "الإعدادات"],
              ["action.new_chat", "New Chat", "محادثة جديدة"],
              ["action.new_group", "New Group", "مجموعة جديدة"],
              ["action.send", "Send", "إرسال"],
              ["action.call", "Call", "اتصال"],
              ["action.video_call", "Video Call", "مكالمة فيديو"],
              ["action.share_screen", "Share Screen", "مشاركة الشاشة"],
              ["action.stop_sharing", "Stop Sharing", "إيقاف المشاركة"],
              ["action.accept", "Answer", "رد"],
              ["action.decline", "Decline", "رفض"],
              ["action.end_call", "End Call", "إنهاء المكالمة"],
              ["action.mute", "Mute", "كتم"],
              ["action.unmute", "Unmute", "إلغاء الكتم"],
              ["action.log_out", "Log Out", "تسجيل الخروج"],
              ["status.online", "Online", "متصل"],
              ["status.offline", "Offline", "غير متصل"],
              ["status.connecting", "Connecting...", "جارٍ الاتصال..."],
              ["status.calling", "Calling...", "جارٍ الاتصال..."],
              ["status.in_call", "In a call", "في مكالمة"],
              ["status.typing", "typing...", "يكتب..."],
              ["status.sent", "Sent", "تم الإرسال"],
              ["status.delivered", "Delivered", "تم التوصيل"],
              ["status.read", "Read", "مقروءة"],
              ["confirm.leave_group", "Leave this group?", "مغادرة المجموعة؟"],
              ["confirm.delete_message", "Delete this message?", "حذف هذه الرسالة؟"],
              ["confirm.end_call", "End this call?", "إنهاء المكالمة؟"],
              ["confirm.log_out", "Log out?", "تسجيل الخروج؟"],
              ["error.no_connection", "Can't reach the server", "لا يمكن الوصول للسيرفر"],
              ["error.session_expired", "Please log in again", "الرجاء تسجيل الدخول مرة أخرى"],
              ["error.too_fast", "Wait a moment and try again", "انتظر لحظة وحاول مرة أخرى"],
              ["error.mic_blocked", "Microphone not working", "الميكروفون لا يعمل"],
              ["error.call_failed", "Call couldn't connect", "تعذر الاتصال"],
              ["error.file_too_big", "File is too big", "الملف كبير جداً"],
            ].map(([key, en, ar]) => (
              <tr key={key} style={{ borderBottom: `1px solid ${COLORS.border}22` }}>
                <td style={{ padding: "6px 8px", fontFamily: "monospace", fontSize: 11, color: COLORS.textDim }}>{key}</td>
                <td style={{ padding: "6px 8px" }}>{en}</td>
                <td style={{ padding: "6px 8px", textAlign: "right", direction: "rtl" }}>{ar}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

// ── MAIN APP ─────────────────────────────────────────────────

const SCREENS = [
  { id: "arch", label: "1. UX Architecture", icon: "📐" },
  { id: "welcome", label: "2. Welcome", icon: "👋" },
  { id: "login", label: "3. Login", icon: "🔑" },
  { id: "register", label: "4. Register", icon: "🎉" },
  { id: "home", label: "5. Home", icon: "🏠" },
  { id: "chat", label: "6. Chat", icon: "💬" },
  { id: "incoming", label: "7. Incoming Call", icon: "📞" },
  { id: "active-call", label: "8. Active Call", icon: "🎤" },
  { id: "group-call", label: "9. Group Call", icon: "👥" },
  { id: "settings", label: "10. Settings", icon: "⚙️" },
  { id: "errors", label: "11. Error Messages", icon: "⚠️" },
  { id: "confirms", label: "12. Confirmations", icon: "✅" },
  { id: "components", label: "13. Components", icon: "🧩" },
];

export default function App() {
  const [screen, setScreen] = useState("arch");

  const renderScreen = useCallback(() => {
    switch (screen) {
      case "arch": return <ArchitectureSpec />;
      case "welcome": return <WelcomeScreen onNext={() => setScreen("register")} />;
      case "login": return <LoginScreen />;
      case "register": return <RegisterScreen />;
      case "home": return <HomeScreen onNav={(s) => setScreen(s === "settings" ? "settings" : s === "new-chat" || s === "chat" ? "chat" : "home")} />;
      case "chat": return <ChatScreen onBack={() => setScreen("home")} />;
      case "incoming": return <IncomingCallScreen />;
      case "active-call": return <ActiveCallScreen />;
      case "group-call": return <GroupCallScreen />;
      case "settings": return <SettingsScreen />;
      case "errors": return <ErrorMessages />;
      case "confirms": return <ConfirmationDialogs />;
      case "components": return <ComponentSpec />;
      default: return <ArchitectureSpec />;
    }
  }, [screen]);

  return (
    <div style={{ display: "flex", height: "100vh", background: "#020617", fontFamily: "'Inter', 'Segoe UI', -apple-system, sans-serif", color: COLORS.text }}>
      {/* Left: Screen navigator */}
      <div style={{
        width: 220, background: COLORS.bg, borderRight: `1px solid ${COLORS.border}`,
        overflowY: "auto", padding: "12px 8px", flexShrink: 0,
      }}>
        <div style={{ padding: "8px 12px 16px", borderBottom: `1px solid ${COLORS.border}`, marginBottom: 12 }}>
          <div style={{ fontSize: 14, fontWeight: 800, color: COLORS.primary }}>CommClient</div>
          <div style={{ fontSize: 11, color: COLORS.textMuted }}>UX Redesign Spec</div>
        </div>
        {SCREENS.map(s => (
          <button key={s.id} onClick={() => setScreen(s.id)} style={{
            display: "flex", alignItems: "center", gap: 8, width: "100%",
            padding: "10px 12px", borderRadius: 10, border: "none", cursor: "pointer",
            background: screen === s.id ? COLORS.primary + "22" : "transparent",
            color: screen === s.id ? COLORS.primary : COLORS.textMuted,
            fontSize: 12, fontWeight: screen === s.id ? 700 : 500, textAlign: "left",
            marginBottom: 2, transition: "all 0.15s",
          }}>
            <span style={{ fontSize: 14 }}>{s.icon}</span>
            <span>{s.label}</span>
          </button>
        ))}
      </div>

      {/* Center: Phone mockup */}
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", padding: 24, background: "#020617" }}>
        <div style={{
          width: 400, height: 720, background: COLORS.bg,
          borderRadius: 32, border: `2px solid ${COLORS.border}`,
          overflow: "hidden", display: "flex", flexDirection: "column",
          boxShadow: "0 20px 60px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.05)",
        }}>
          {/* Phone status bar */}
          <div style={{
            height: 28, background: COLORS.bg,
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "0 20px", fontSize: 12, color: COLORS.textMuted, flexShrink: 0,
          }}>
            <span>10:30 AM</span>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <Wifi size={12} color={COLORS.textMuted} />
              <span>100%</span>
            </div>
          </div>
          {/* Screen content */}
          <div style={{ flex: 1, overflow: "hidden" }}>
            {renderScreen()}
          </div>
        </div>
      </div>

      <style>{`
        @keyframes pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.05); } }
        * { box-sizing: border-box; margin: 0; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
        input::placeholder { color: #64748B; }
      `}</style>
    </div>
  );
}
