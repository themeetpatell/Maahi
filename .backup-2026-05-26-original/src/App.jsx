import { useState, useRef, useEffect, useCallback } from "react";

// ─── Markdown renderer ────────────────────────────────────────
function renderMd(text) {
  if (!text) return "";
  return text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="cb"><code>$2</code></pre>')
    .replace(/`([^`]+)`/g, '<code class="ic">$1</code>')
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/^### (.+)$/gm, '<p class="h3">$1</p>')
    .replace(/^## (.+)$/gm, '<p class="h2">$1</p>')
    .replace(/^# (.+)$/gm, '<p class="h1">$1</p>')
    .replace(/^---$/gm, '<hr class="hr"/>')
    .replace(/^[-•] (.+)$/gm, '<p class="li">$1</p>')
    .replace(/^\d+\. (.+)$/gm, '<p class="oli">$1</p>')
    .replace(/\n/g, "<br/>");
}

// ─── Constants ────────────────────────────────────────────────
const MODES = [
  { value: "general",  label: "General",  color: "#38bdf8", g: "rgba(56,189,248,0.07)"  },
  { value: "ops",      label: "Ops",      color: "#f59e0b", g: "rgba(245,158,11,0.07)"  },
  { value: "sales",    label: "Sales",    color: "#10b981", g: "rgba(16,185,129,0.07)"  },
  { value: "soulmap",  label: "Soulmap",  color: "#ec4899", g: "rgba(236,72,153,0.07)"  },
  { value: "writer",   label: "Writer",   color: "#a78bfa", g: "rgba(167,139,250,0.07)" },
  { value: "analyst",  label: "Analyst",  color: "#6366f1", g: "rgba(99,102,241,0.07)"  },
];

const SUGGESTIONS = [
  { label: "Morning briefing",  msg: "Good morning Maahi! Give me my daily briefing." },
  { label: "My open tasks",     msg: "Show me all my open tasks." },
  { label: "Soulmap idea",      msg: "Give me one killer viral feature idea for Soulmap." },
  { label: "Draft an email",    msg: "Help me draft a professional email." },
  { label: "Web research",      msg: "Search web for latest AI companion app trends." },
  { label: "Strategic advice",  msg: "What is the one move I should make this week?" },
];

const SLASH_CMDS = [
  { cmd: "/briefing", desc: "Daily briefing",  msg: "Give me my daily briefing." },
  { cmd: "/tasks",    desc: "Open tasks",      msg: "Show all open tasks." },
  { cmd: "/search",   desc: "Web search",      msg: "Search web for " },
  { cmd: "/note",     desc: "Save note",       msg: "Save note: " },
  { cmd: "/email",    desc: "Draft email",     msg: "Draft email about: " },
  { cmd: "/sop",      desc: "Create SOP",      msg: "Create an SOP for: " },
  { cmd: "/memory",   desc: "My memory",       msg: "What do you remember about me?" },
  { cmd: "/clear",    desc: "New chat",        msg: null },
];

function genId() { return Date.now().toString(36) + Math.random().toString(36).slice(2); }

// ─── Orb ──────────────────────────────────────────────────────
function Orb({ listening, speaking, mode, onClick }) {
  const m = MODES.find(x => x.value === mode) || MODES[0];
  const active = listening || speaking;
  return (
    <div className="orb-wrap" onClick={onClick} title="Click to speak">
      <div className="orb-ring r3" style={{ borderColor: `${m.color}18`, background: `${m.color}05` }} />
      <div className="orb-ring r2" style={{ borderColor: `${m.color}28`, background: `${m.color}08` }} />
      <div className="orb-ring r1" style={{ borderColor: `${m.color}45`, background: `${m.color}0f` }} />
      {active && <>
        <div className="orb-pulse" style={{ borderColor: `${listening ? "#fb7185" : m.color}60` }} />
        <div className="orb-pulse p2" style={{ borderColor: `${listening ? "#fb7185" : m.color}30` }} />
      </>}
      <div className="orb-core" style={{
        background: listening
          ? "radial-gradient(circle at 38% 30%, #fb7185, #be185d)"
          : `radial-gradient(circle at 38% 30%, ${m.color}ee, ${m.color}88)`,
        boxShadow: `0 0 28px ${listening ? "#fb718555" : m.color + "44"}, inset 0 1px 0 rgba(255,255,255,0.25)`,
      }}>
        {listening
          ? <div className="bars">{[0,1,2,3,4].map(i=><div key={i} className="bar" style={{animationDelay:`${i*0.1}s`}}/>)}</div>
          : speaking ? "◉" : "◎"}
      </div>
    </div>
  );
}

// ─── Sidebar ──────────────────────────────────────────────────
function Sidebar({ conversations, activeId, onSelect, onNew, onDelete, onClose }) {
  return (
    <div className="sidebar">
      <div className="sb-head">
        <span className="sb-title">Conversations</span>
        <div style={{ display:"flex", gap:6 }}>
          <button className="sb-btn" onClick={onNew}>+ New</button>
          <button className="sb-btn" onClick={onClose}>&#x2715;</button>
        </div>
      </div>
      <div className="sb-list">
        {conversations.length === 0
          ? <div className="sb-empty">No saved conversations</div>
          : conversations.map(c => (
            <div key={c.id} className={`sb-item ${c.id===activeId?"active":""}`} onClick={()=>onSelect(c.id)}>
              <div className="sb-item-name">{c.title||"Untitled"}</div>
              <div className="sb-item-meta">{c.count} msgs &middot; {new Date(c.updatedAt).toLocaleDateString()}</div>
              <button className="sb-del" onClick={e=>{e.stopPropagation();onDelete(c.id);}}>&#x2715;</button>
            </div>
          ))}
      </div>
    </div>
  );
}

// ─── Contacts Modal ───────────────────────────────────────────
function ContactsModal({ onClose }) {
  const [contacts, setContacts] = useState([]);
  const [busy, setBusy] = useState(true);
  const [search, setSearch] = useState("");
  useEffect(() => {
    fetch("/api/contacts").then(r=>r.json()).then(d=>{setContacts(d.contacts||[]);setBusy(false);}).catch(()=>setBusy(false));
  }, []);
  const del = async (name) => {
    await fetch(`/api/contacts/${encodeURIComponent(name)}`,{method:"DELETE"});
    setContacts(prev=>prev.filter(c=>c.name!==name));
  };
  const filtered = contacts.filter(c =>
    !search || c.name.toLowerCase().includes(search.toLowerCase()) ||
    c.company?.toLowerCase().includes(search.toLowerCase()) ||
    c.relationship?.toLowerCase().includes(search.toLowerCase())
  );
  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={e=>e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="modal-title">Contacts</div>
            <div className="modal-sub">{contacts.length} people</div>
          </div>
          <button className="icon-btn sm" onClick={onClose}>&#x2715;</button>
        </div>
        <input className="modal-search" placeholder="Search contacts..." value={search} onChange={e=>setSearch(e.target.value)}/>
        {busy ? <div className="modal-empty">Loading...</div>
          : filtered.length===0 ? <div className="modal-empty">{search?"No results.":"No contacts yet. Mention someone in chat!"}</div>
          : <div className="mem-list">
              {filtered.map((c,i)=>(
                <div key={i} className="contact-item">
                  <div className="contact-avatar">{c.name[0].toUpperCase()}</div>
                  <div className="contact-info">
                    <div className="contact-name">{c.name}{c.company&&<span className="contact-co"> @ {c.company}</span>}</div>
                    {c.relationship&&<div className="mem-cat">{c.relationship}</div>}
                    {c.email&&<div className="contact-detail">✉ {c.email}</div>}
                    {c.phone&&<div className="contact-detail">📞 {c.phone}</div>}
                    {c.notes&&<div className="contact-notes">{c.notes}</div>}
                  </div>
                  <button className="sb-del" onClick={()=>del(c.name)}>&#x2715;</button>
                </div>
              ))}
            </div>}
      </div>
    </div>
  );
}

// ─── Memory Modal ─────────────────────────────────────────────
function MemoryModal({ onClose }) {
  const [mem, setMem] = useState({ facts:{}, categories:{} });
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    fetch("/api/memory").then(r=>r.json()).then(d=>{setMem(d);setBusy(false);}).catch(()=>setBusy(false));
  }, []);
  const clearAll = async () => {
    if (!window.confirm("Clear all stored memory?")) return;
    await fetch("/api/memory",{method:"DELETE"});
    setMem({facts:{},categories:{}});
  };
  const facts = Object.entries(mem.facts||{});
  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={e=>e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="modal-title">Memory</div>
            <div className="modal-sub">{facts.length} stored facts</div>
          </div>
          <button className="icon-btn sm" onClick={onClose}>&#x2715;</button>
        </div>
        {busy ? <div className="modal-empty">Loading...</div>
          : facts.length===0 ? <div className="modal-empty">No memories yet. Start chatting!</div>
          : <div className="mem-list">
              {facts.map(([k,v],i)=>(
                <div key={i} className="mem-item">
                  <div className="mem-k">{k.replace(/_/g," ")}</div>
                  <div className="mem-v">{typeof v==="object"?v.value:v}</div>
                  {typeof v==="object"&&v.category&&<div className="mem-cat">{v.category}</div>}
                </div>
              ))}
            </div>}
        <button className="btn-danger" onClick={clearAll}>Clear all memory</button>
      </div>
    </div>
  );
}

// ─── App ──────────────────────────────────────────────────────
export default function App() {
  const [convoId,        setConvoId]       = useState(genId);
  const [messages,       setMessages]      = useState([{role:"assistant",content:"Hey Meet. I am here \u2014 what's on your mind?"}]);
  const [input,          setInput]         = useState("");
  const [loading,        setLoading]       = useState(false);
  const [listening,      setListening]     = useState(false);
  const [speaking,       setSpeaking]      = useState(false);
  const [voiceMode,      setVoiceMode]     = useState(false);
  const [mode,           setMode]          = useState("general");
  const [toolCalls,      setToolCalls]     = useState([]);
  const [showMemory,     setShowMemory]    = useState(false);
  const [showSidebar,    setShowSidebar]   = useState(false);
  const [modeOpen,       setModeOpen]      = useState(false);
  const [conversations,  setConversations] = useState([]);
  const [slashOpen,      setSlashOpen]     = useState(false);
  const [slashQ,         setSlashQ]        = useState("");
  const [attachedFiles,  setAttachedFiles] = useState([]);
  const [notifications,  setNotifications] = useState([]);
  const [showNotif,      setShowNotif]     = useState(false);
  const [showContacts,   setShowContacts]  = useState(false);
  const [contacts,       setContacts]      = useState([]);
  const [generatedImages,setGeneratedImages] = useState({}); // msgIndex -> {mimeType,data}
  const [googleConnected, setGoogleConnected] = useState(false);
  const [showGoogleMenu,  setShowGoogleMenu]  = useState(false);
  const fileInputRef = useRef(null);

  const bottomRef = useRef(null);
  const inputRef  = useRef(null);
  const recRef    = useRef(null);
  const synth     = useRef(window.speechSynthesis);
  const voices    = useRef([]);

  const modeObj = MODES.find(m=>m.value===mode)||MODES[0];
  const isEmpty  = messages.length <= 1;

  useEffect(() => {
    const load = () => { voices.current = synth.current.getVoices(); };
    load(); synth.current.onvoiceschanged = load;
  }, []);

  useEffect(() => { bottomRef.current?.scrollIntoView({behavior:"smooth"}); }, [messages, loading, toolCalls]);

  const loadConvos = useCallback(async () => {
    try { const r=await fetch("/api/conversations"); const d=await r.json(); setConversations(d.conversations||[]); } catch{}
  },[]);
  useEffect(()=>{loadConvos();},[loadConvos]);

  // Poll for notifications every 30s
  useEffect(()=>{
    const poll = async()=>{
      try{ const r=await fetch("/api/notifications"); const d=await r.json(); setNotifications(d.notifications||[]); }catch{}
    };
    poll();
    const t=setInterval(poll,30000);
    return ()=>clearInterval(t);
  },[]);

  const loadContacts = useCallback(async()=>{
    try{ const r=await fetch("/api/contacts"); const d=await r.json(); setContacts(d.contacts||[]); }catch{}
  },[]);
  useEffect(()=>{ loadContacts(); },[loadContacts]);

  // Check Google OAuth status on load
  useEffect(()=>{
    fetch("/api/auth/google/status").then(r=>r.json()).then(d=>setGoogleConnected(d.connected||false)).catch(()=>{});
  },[]);

  const exportPDF = (content, title="Maahi Export") => {
    const w = window.open("","_blank");
    w.document.write(`<!DOCTYPE html><html><head><title>${title}</title><style>
      body{font-family:system-ui,sans-serif;max-width:800px;margin:40px auto;padding:0 24px;color:#1e293b;line-height:1.7}
      h1,h2,h3{color:#0f172a} pre{background:#f1f5f9;padding:16px;border-radius:8px;overflow-x:auto}
      code{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:13px}
      @media print{body{margin:20px}}
    </style></head><body>
      <h1>${title}</h1>
      <div>${renderMd(content)}</div>
      <p style="color:#94a3b8;font-size:12px;margin-top:40px">Generated by Maahi · ${new Date().toLocaleDateString()}</p>
    </body></html>`);
    w.document.close();
    setTimeout(()=>w.print(), 500);
  };

  useEffect(()=>{
    if (messages.length<=1) return;
    const title = messages.find(m=>m.role==="user")?.content?.slice(0,60)||"Untitled";
    const t = setTimeout(()=>{
      fetch("/api/conversations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:convoId,title,messages})})
        .then(()=>loadConvos()).catch(()=>{});
    },1500);
    return ()=>clearTimeout(t);
  },[messages,convoId,loadConvos]);

  const speakText = useCallback((text)=>{
    const s=synth.current; s.cancel();
    const clean=text.replace(/[*_`#<>[\]]/g,"").replace(/\n/g," ").trim();
    if (!clean) return;
    const u=new SpeechSynthesisUtterance(clean);
    u.rate=0.95; u.pitch=1.1; u.volume=1;
    const v=voices.current; const voice=v.find(x=>x.lang?.startsWith("en-IN"))||v.find(x=>x.lang?.startsWith("en"))||v[0];
    if (voice) u.voice=voice;
    u.onstart=()=>setSpeaking(true); u.onend=()=>setSpeaking(false); u.onerror=()=>setSpeaking(false);
    s.speak(u);
  },[]);

  const toggleVoice = ()=>{
    if (!("webkitSpeechRecognition" in window||"SpeechRecognition" in window)) return;
    if (listening){recRef.current?.stop();setListening(false);return;}
    const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
    const r=new SR(); r.lang="en-IN"; r.continuous=false; r.interimResults=false;
    r.onstart=()=>setListening(true);
    r.onresult=e=>{setListening(false);sendMessage(e.results[0][0].transcript);};
    r.onerror=()=>setListening(false); r.onend=()=>setListening(false);
    recRef.current=r; r.start();
  };

  const handleFileAttach = (e) => {
    const fileList = Array.from(e.target.files||[]);
    fileList.forEach(file=>{
      const reader = new FileReader();
      reader.onload = ev => {
        const data = ev.target.result.split(",")[1]; // base64 only
        setAttachedFiles(prev=>[...prev,{name:file.name,mimeType:file.type,data,size:file.size}]);
      };
      reader.readAsDataURL(file);
    });
    e.target.value="";
  };

  const sendMessage = async (text) => {
    if ((!text?.trim()&&attachedFiles.length===0)||loading) return;
    const displayText = text?.trim()||(attachedFiles.length>0?`[${attachedFiles.map(f=>f.name).join(", ")}]`:"");
    const userMsg={role:"user",content:displayText};
    const filesToSend = [...attachedFiles];
    setMessages(prev=>[...prev,userMsg,{role:"assistant",content:""}]);
    setInput(""); setAttachedFiles([]); setToolCalls([]); setLoading(true); setSlashOpen(false);
    if(inputRef.current){inputRef.current.style.height="auto";}

    try {
      const res = await fetch("/api/chat/stream",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({messages:[...messages,userMsg],mode,max_tokens:2048,files:filesToSend}),
      });
      if (!res.ok||!res.body) throw new Error(`HTTP ${res.status}`);

      const reader=res.body.getReader(); const dec=new TextDecoder();
      let buf="", raw="";

      while(true){
        const {done,value}=await reader.read();
        if (done) break;
        buf+=dec.decode(value,{stream:true});
        const chunks=buf.split("\n\n"); buf=chunks.pop()||"";
        for (const chunk of chunks){
          const dl=chunk.split("\n").find(l=>l.startsWith("data:"));
          if (!dl) continue;
          let p; try{p=JSON.parse(dl.replace(/^data:\s*/,""));}catch{continue;}
          if (p.type==="token"){
            raw+=p.text||"";
            setMessages(prev=>{const n=[...prev];const i=n.length-1;if(i>=0&&n[i].role==="assistant")n[i]={...n[i],content:raw};return n;});
          }
          if (p.type==="image"){
            setMessages(prev=>{const n=[...prev];const i=n.length-1;if(i>=0&&n[i].role==="assistant")n[i]={...n[i],image:{mimeType:p.mimeType,data:p.data}};return n;});
          }
          if (p.type==="tool_call")   setToolCalls(prev=>[...prev,{name:p.name,input:p.input,done:false}]);
          if (p.type==="tool_result") setToolCalls(prev=>prev.map(tc=>tc.name===p.name&&!tc.done?{...tc,result:p.result,done:true}:tc));
          if (p.type==="done") raw=p.text||raw;
          if (p.type==="error") throw new Error(p.message);
        }
      }

      setMessages(prev=>{const n=[...prev];const i=n.length-1;if(i>=0&&n[i].role==="assistant")n[i]={...n[i],content:raw};return n;});
      if (voiceMode&&raw) speakText(raw);
    } catch(e){
      setMessages(prev=>{
        const n=prev.filter((m,i)=>!(i===prev.length-1&&m.role==="assistant"&&!m.content));
        return [...n,{role:"assistant",content:`Something went wrong: ${e.message}`}];
      });
    }
    setLoading(false);
  };

  const newConvo = ()=>{
    setConvoId(genId());
    setMessages([{role:"assistant",content:"New conversation. What's on your mind, Meet?"}]);
    setToolCalls([]); setShowSidebar(false);
  };

  const loadConvo = async (id)=>{
    try{const r=await fetch(`/api/conversations/${id}`);const c=await r.json();if(c?.messages){setConvoId(c.id);setMessages(c.messages);setToolCalls([]);setShowSidebar(false);}}catch{}
  };

  const deleteConvo = async (id)=>{
    await fetch(`/api/conversations/${id}`,{method:"DELETE"});
    if (id===convoId) newConvo();
    loadConvos();
  };

  const handleInput=(e)=>{
    const val=e.target.value; setInput(val);
    setSlashOpen(val.startsWith("/"));
    setSlashQ(val.startsWith("/")?val.slice(1).toLowerCase():"");
    e.target.style.height="auto";
    e.target.style.height=Math.min(e.target.scrollHeight,120)+"px";
  };

  const handleKey=(e)=>{
    if (e.key==="Enter"&&!e.shiftKey){
      e.preventDefault();
      const slash=SLASH_CMDS.find(s=>input.toLowerCase().startsWith(s.cmd));
      if (slash){if(slash.cmd==="/clear"){newConvo();setInput("");return;}sendMessage(slash.msg+input.slice(slash.cmd.length).trim());}
      else sendMessage(input);
    }
  };

  const pickSlash=(cmd)=>{
    if(cmd.cmd==="/clear"){newConvo();setSlashOpen(false);setInput("");return;}
    setInput(cmd.msg);setSlashOpen(false);inputRef.current?.focus();
  };

  const filteredSlash=SLASH_CMDS.filter(s=>!slashQ||s.cmd.slice(1).includes(slashQ)||s.desc.toLowerCase().includes(slashQ));

  return (
    <div className="app" style={{"--c":modeObj.color,"--g":modeObj.g}}>
      <style>{CSS}</style>

      <div className="ambient"/>

      {showSidebar&&<>
        <div className="overlay-bg" onClick={()=>setShowSidebar(false)}/>
        <Sidebar conversations={conversations} activeId={convoId} onSelect={loadConvo} onNew={newConvo} onDelete={deleteConvo} onClose={()=>setShowSidebar(false)}/>
      </>}

      {/* Header */}
      <header className="header">
        <div className="h-left">
          <button className="icon-btn" onClick={()=>setShowSidebar(s=>!s)}>
            <svg width="17" height="13" fill="none" xmlns="http://www.w3.org/2000/svg">
              <rect width="17" height="1.8" rx=".9" fill="currentColor"/>
              <rect y="5.1" width="12" height="1.8" rx=".9" fill="currentColor"/>
              <rect y="10.2" width="17" height="1.8" rx=".9" fill="currentColor"/>
            </svg>
          </button>
          <div className="logo-dot" style={{background:`radial-gradient(circle at 35% 30%, var(--c), ${modeObj.color}77)`}}>M</div>
          <div>
            <div className="logo-name">MAAHI</div>
            <div className="logo-sub" style={{color:"var(--c)"}}>
              {listening?"LISTENING":speaking?"SPEAKING":modeObj.label}
            </div>
          </div>
        </div>
        <div className="h-right">
          <div style={{position:"relative"}}>
            <button className="h-pill mode-pill" onClick={()=>setModeOpen(o=>!o)} style={{color:"var(--c)",borderColor:`${modeObj.color}40`}}>
              {modeObj.label}&nbsp;<span style={{fontSize:8,opacity:.6}}>&#9662;</span>
            </button>
            {modeOpen&&(
              <div className="mode-drop">
                {MODES.map(m2=>(
                  <button key={m2.value} className="mode-opt" onClick={()=>{setMode(m2.value);setModeOpen(false);}}
                    style={{color:mode===m2.value?m2.color:"#64748b",background:mode===m2.value?`${m2.color}18`:"transparent"}}>
                    <span className="dot" style={{background:m2.color}}/>{m2.label}
                  </button>
                ))}
              </div>
            )}
          </div>
          <button className="h-pill" onClick={()=>setVoiceMode(v=>!v)} style={voiceMode?{color:"var(--c)",borderColor:`${modeObj.color}40`}:{}}>
            {voiceMode?"Voice On":"Voice"}
          </button>
          <div style={{position:"relative"}}>
            <button className="h-pill" onClick={()=>setShowGoogleMenu(o=>!o)}
              style={googleConnected?{color:"#34d399",borderColor:"#34d39940"}:{}}>
              <span style={{display:"inline-flex",alignItems:"center",gap:5}}>
                <span style={{width:6,height:6,borderRadius:"50%",background:googleConnected?"#34d399":"#475569",display:"inline-block"}}/>
                Google
              </span>
            </button>
            {showGoogleMenu&&(
              <div className="mode-drop" onClick={e=>e.stopPropagation()} style={{minWidth:180}}>
                <div style={{padding:"8px 12px 4px",fontSize:11,color:"#64748b",textTransform:"uppercase",letterSpacing:1}}>
                  {googleConnected?"Connected":"Not connected"}
                </div>
                {googleConnected ? <>
                  <div style={{padding:"6px 12px",fontSize:12,color:"#94a3b8"}}>Gmail &amp; Calendar active</div>
                  <button className="mode-opt" style={{color:"#f87171"}} onClick={async()=>{
                    await fetch("/api/auth/google",{method:"DELETE"});
                    setGoogleConnected(false); setShowGoogleMenu(false);
                  }}>Disconnect</button>
                </> : <>
                  <div style={{padding:"6px 12px",fontSize:12,color:"#94a3b8"}}>Connect to use Gmail &amp; Calendar</div>
                  <button className="mode-opt" style={{color:"#34d399"}} onClick={()=>{
                    setShowGoogleMenu(false);
                    window.open("/api/auth/google","_blank","width=500,height=600");
                    // poll for connection after redirect
                    const interval=setInterval(()=>{
                      fetch("/api/auth/google/status").then(r=>r.json()).then(d=>{
                        if(d.connected){setGoogleConnected(true);clearInterval(interval);}
                      }).catch(()=>{});
                    },2000);
                    setTimeout(()=>clearInterval(interval),120000);
                  }}>Connect Google</button>
                </>}
              </div>
            )}
          </div>
          <button className="h-pill" onClick={()=>setShowContacts(true)}>People</button>
          <button className="h-pill" onClick={()=>setShowMemory(true)}>Memory</button>
          <div style={{position:"relative"}}>
            <button className="icon-btn notif-btn" onClick={async()=>{setShowNotif(o=>!o);if(!showNotif&&notifications.length>0){await fetch("/api/notifications/read",{method:"POST"});setNotifications([]);}}}
              style={{color:notifications.length>0?"#fb7185":"#64748b",position:"relative"}}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>
              </svg>
              {notifications.length>0&&<span className="notif-badge">{notifications.length}</span>}
            </button>
            {showNotif&&(
              <div className="notif-drop" onClick={e=>e.stopPropagation()}>
                <div className="notif-head">Notifications</div>
                {notifications.length===0
                  ? <div className="notif-empty">All caught up!</div>
                  : notifications.map(n=>(
                    <div key={n.id} className="notif-item">
                      <span className="notif-icon">🔔</span>
                      <div>
                        <div className="notif-txt">{n.text}</div>
                        <div className="notif-time">{new Date(n.createdAt).toLocaleTimeString("en-US",{hour:"2-digit",minute:"2-digit",hour12:true})}</div>
                      </div>
                    </div>
                  ))}
              </div>
            )}
          </div>
          <button className="h-pill" onClick={newConvo}>New</button>
        </div>
      </header>

      {/* Content */}
      <div className="content">
        {isEmpty ? (
          <div className="empty">
            <div className="orb-section">
              <Orb listening={listening} speaking={speaking} mode={mode} onClick={toggleVoice}/>
              <div className="orb-lbl">{listening?"LISTENING\u2026":speaking?"SPEAKING\u2026":"TAP TO SPEAK"}</div>
            </div>
            <div className="welcome-txt">{messages[0].content}</div>
            <div className="chips-row">
              {SUGGESTIONS.map((s,i)=>(
                <button key={i} className="chip" onClick={()=>sendMessage(s.msg)}>{s.label}</button>
              ))}
            </div>
          </div>
        ) : (
          <div className="msgs">
            {messages.map((m,i)=>(
              <div key={i} className={`row ${m.role==="user"?"user":"ai"}`}>
                {m.role==="assistant"&&(
                  <div className="ava ai-ava" style={{background:`radial-gradient(circle at 38% 30%, var(--c), ${modeObj.color}77)`}}>M</div>
                )}
                <div className={`bubble ${m.role==="user"?"ub":"ab"}`}>
                  {m.content
                    ? <div dangerouslySetInnerHTML={{__html:renderMd(m.content)}}/>
                    : m.role==="assistant"&&loading&&i===messages.length-1
                      ? <div className="dots"><span/><span/><span/></div>
                      : null}
                  {m.image&&(
                    <div className="gen-img-wrap">
                      <img src={`data:${m.image.mimeType};base64,${m.image.data}`} alt="Generated" className="gen-img"/>
                      <a className="img-dl" href={`data:${m.image.mimeType};base64,${m.image.data}`} download="maahi-image.png">↓ Download</a>
                    </div>
                  )}
                  {m.role==="assistant"&&m.content&&m.content.length>100&&(
                    <button className="export-btn" onClick={()=>exportPDF(m.content,"Maahi Note")} title="Export as PDF">↗ Export PDF</button>
                  )}
                </div>
                {m.role==="user"&&<div className="ava u-ava">M</div>}
              </div>
            ))}

            {toolCalls.length>0&&(
              <div className="tool-row">
                {toolCalls.map((tc,i)=>(
                  <div key={i} className={`tool-pill ${tc.done?"tdone":"tactive"}`}>
                    <span className="tdot"/>
                    <span>{tc.name.replace(/_/g," ")}</span>
                    {tc.done&&<span className="tick">&#10003;</span>}
                  </div>
                ))}
              </div>
            )}

            <div ref={bottomRef}/>
          </div>
        )}
      </div>

      {/* Input */}
      <div className="input-area">
        <div className="input-box" style={{borderColor:`${modeObj.color}30`}}>
          {slashOpen&&filteredSlash.length>0&&(
            <div className="slash-menu">
              {filteredSlash.map((s,i)=>(
                <div key={i} className="slash-item" onClick={()=>pickSlash(s)}>
                  <span className="slash-cmd">{s.cmd}</span>
                  <span className="slash-desc">{s.desc}</span>
                </div>
              ))}
            </div>
          )}
          <div className="input-inner">
            {attachedFiles.length>0&&(
              <div className="file-chips">
                {attachedFiles.map((f,i)=>(
                  <div key={i} className="file-chip">
                    <span className="file-chip-icon">{f.mimeType.startsWith("image/")?"🖼":"📄"}</span>
                    <span className="file-chip-name">{f.name.length>18?f.name.slice(0,15)+"...":f.name}</span>
                    <button className="file-chip-rm" onClick={()=>setAttachedFiles(prev=>prev.filter((_,j)=>j!==i))}>×</button>
                  </div>
                ))}
              </div>
            )}
            <div className="inp-row">
              <input ref={fileInputRef} type="file" style={{display:"none"}} multiple
                accept="image/*,.pdf,.txt,.csv,.md,.json" onChange={handleFileAttach}/>
              <button className="icon-btn attach-btn" onClick={()=>fileInputRef.current?.click()} title="Attach file">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
                </svg>
              </button>
              <textarea ref={inputRef} value={input} onChange={handleInput} onKeyDown={handleKey}
                placeholder="Ask Maahi anything..." rows={1} className="inp"/>
              <div className="inp-btns">
                <button className={`icon-btn mic-btn ${listening?"mic-on":""}`} onClick={toggleVoice}
                  style={{color:listening?"#fb7185":"var(--c)"}}>
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <rect x="5" y="1" width="6" height="9" rx="3" fill="currentColor" opacity=".85"/>
                    <path d="M2 7.5C2 10.54 4.69 13 8 13s6-2.46 6-5.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                    <line x1="8" y1="13" x2="8" y2="15.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  </svg>
                </button>
                <button className="send-btn" onClick={()=>sendMessage(input)} disabled={loading||(!input.trim()&&attachedFiles.length===0)}
                  style={{background:(input.trim()||attachedFiles.length>0)?`linear-gradient(135deg,var(--c),${modeObj.color}bb)`:"rgba(255,255,255,0.06)"}}>
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M7 12V2M2 7l5-5 5 5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </div>
        <div className="footer-lbl">
          Maahi&nbsp;&middot;&nbsp;Gemini 2.5 Flash&nbsp;&middot;&nbsp;/ for commands
        </div>
      </div>

      {showMemory&&<MemoryModal onClose={()=>setShowMemory(false)}/>}
      {showContacts&&<ContactsModal onClose={()=>setShowContacts(false)}/>}
    </div>
  );
}

// ─── Styles ───────────────────────────────────────────────────
const CSS = `
*{box-sizing:border-box;margin:0;padding:0}
button{font-family:inherit;cursor:pointer}
textarea{font-family:inherit}

.app{
  min-height:100vh;
  background:#070c18;
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  display:flex;flex-direction:column;
  color:#e2e8f0;position:relative;overflow:hidden;
}

.ambient{
  position:fixed;top:50%;left:50%;
  transform:translate(-50%,-50%);
  width:900px;height:900px;border-radius:50%;
  background:radial-gradient(closest-side,var(--g,rgba(56,189,248,0.07)),transparent);
  pointer-events:none;z-index:0;
  transition:background 1.2s ease;
  animation:drift 14s ease-in-out infinite;
}
@keyframes drift{
  0%,100%{transform:translate(-50%,-50%) scale(1)}
  33%{transform:translate(-47%,-53%) scale(1.06)}
  66%{transform:translate(-53%,-47%) scale(0.96)}
}

::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.07);border-radius:4px}
textarea:focus,select:focus,button:focus{outline:none}

.header{
  position:sticky;top:0;z-index:20;
  height:52px;padding:0 16px;
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(7,12,24,0.88);
  backdrop-filter:blur(24px);
  border-bottom:1px solid rgba(255,255,255,0.05);
  flex-shrink:0;
}
.h-left{display:flex;align-items:center;gap:10px}
.h-right{display:flex;align-items:center;gap:6px}

.icon-btn{
  width:32px;height:32px;border-radius:8px;
  border:none;background:transparent;color:#64748b;
  display:flex;align-items:center;justify-content:center;
  transition:all .2s;flex-shrink:0;
}
.icon-btn:hover{color:#94a3b8;background:rgba(255,255,255,0.06)}
.icon-btn.sm{width:28px;height:28px}

.logo-dot{
  width:32px;height:32px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;
  font-size:13px;font-weight:900;color:#fff;flex-shrink:0;
}
.logo-name{font-weight:800;font-size:14px;letter-spacing:1.5px;color:#f8fafc;line-height:1}
.logo-sub{font-size:8.5px;letter-spacing:2px;text-transform:uppercase;line-height:1;margin-top:2px;transition:color .6s}

.h-pill{
  padding:5px 13px;border-radius:20px;
  border:1px solid rgba(255,255,255,0.08);
  background:rgba(255,255,255,0.04);
  color:#94a3b8;font-size:11px;font-weight:600;
  transition:all .2s;white-space:nowrap;
}
.h-pill:hover{background:rgba(255,255,255,0.09);color:#d1d5db}
.mode-pill{font-weight:700}

.mode-drop{
  position:absolute;top:calc(100% + 8px);right:0;
  background:#0c1525;
  border:1px solid rgba(255,255,255,0.08);
  border-radius:14px;padding:5px;
  min-width:130px;display:flex;flex-direction:column;gap:2px;
  z-index:50;
  box-shadow:0 20px 60px rgba(0,0,0,0.6);
  animation:dropIn .15s ease;
}
@keyframes dropIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
.mode-opt{
  display:flex;align-items:center;gap:8px;
  padding:8px 12px;border-radius:9px;border:none;
  font-size:12px;font-weight:600;text-align:left;
  transition:all .15s;
}
.mode-opt:hover{background:rgba(255,255,255,0.07) !important}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}

.content{flex:1;overflow-y:auto;position:relative;z-index:1}

.empty{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  min-height:calc(100vh - 52px - 130px);
  padding:24px 20px;gap:28px;
}
.orb-section{display:flex;flex-direction:column;align-items:center;gap:14px}

.orb-wrap{
  position:relative;width:130px;height:130px;
  display:flex;align-items:center;justify-content:center;
}
.orb-ring{
  position:absolute;border-radius:50%;border:1px solid;
  transition:border-color .5s,background .5s;
}
.r3{width:130px;height:130px;animation:rpulse 4s ease-in-out infinite}
.r2{width:104px;height:104px;animation:rpulse 4s ease-in-out infinite .6s}
.r1{width:80px;height:80px;animation:rpulse 4s ease-in-out infinite 1.2s}
@keyframes rpulse{0%,100%{transform:scale(1);opacity:.7}50%{transform:scale(1.03);opacity:1}}

.orb-core{
  width:60px;height:60px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:18px;color:rgba(255,255,255,.9);
  transition:all .4s;position:relative;z-index:2;
  border:1px solid rgba(255,255,255,0.2);
}
.orb-pulse{
  position:absolute;width:100%;height:100%;border-radius:50%;
  border:1.5px solid;z-index:1;
  animation:pout 1.8s ease-out infinite;pointer-events:none;
}
.orb-pulse.p2{animation-delay:.7s}
@keyframes pout{from{transform:scale(.85);opacity:1}to{transform:scale(1.7);opacity:0}}

.bars{display:flex;align-items:center;gap:2.5px;height:22px}
.bar{width:3px;background:#fff;border-radius:3px;animation:bb .65s ease-in-out infinite alternate}
@keyframes bb{from{height:5px}to{height:20px}}

.orb-lbl{font-size:9px;color:#475569;letter-spacing:2.5px;font-weight:600;text-transform:uppercase}

.welcome-txt{
  font-size:21px;color:#94a3b8;text-align:center;
  font-weight:300;letter-spacing:-.3px;max-width:500px;line-height:1.55;
}

.chips-row{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;max-width:540px}
.chip{
  padding:8px 16px;border-radius:20px;
  border:1px solid rgba(255,255,255,0.07);
  background:rgba(255,255,255,0.025);
  color:#94a3b8;font-size:12px;font-weight:500;
  transition:all .22s;white-space:nowrap;
}
.chip:hover{border-color:var(--c);color:#e2e8f0;background:rgba(255,255,255,0.06);transform:translateY(-1px)}

.msgs{
  max-width:740px;width:100%;margin:0 auto;
  padding:20px 20px 28px;
  display:flex;flex-direction:column;gap:4px;
}
.row{display:flex;gap:10px;align-items:flex-start;animation:fadeUp .22s ease forwards}
.row.user{flex-direction:row-reverse}
@keyframes fadeUp{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}

.ava{
  width:28px;height:28px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:800;flex-shrink:0;margin-top:4px;color:#fff;
}
.u-ava{background:linear-gradient(135deg,#1e3a8a,#3b82f6)}

.bubble{max-width:76%;padding:10px 15px;font-size:14px;line-height:1.75;border-radius:18px}
.ab{
  background:rgba(255,255,255,0.035);
  border:1px solid rgba(255,255,255,0.045);
  border-top-left-radius:4px;color:#d1d5db;
}
.ub{background:linear-gradient(135deg,#1e40af,#3b82f6);border-bottom-right-radius:4px;color:#fff}

.bubble strong{color:#7dd3fc}
.bubble em{color:#fbbf24;font-style:italic}
.bubble .h1{font-size:17px;font-weight:800;margin:10px 0 5px;color:#f8fafc;display:block}
.bubble .h2{font-size:15px;font-weight:700;margin:8px 0 4px;color:#f1f5f9;display:block}
.bubble .h3{font-size:13px;font-weight:700;margin:5px 0 3px;color:#e2e8f0;display:block}
.bubble .li{padding-left:14px;position:relative;margin:3px 0;display:block}
.bubble .li::before{content:"\u2013";position:absolute;left:0;color:var(--c)}
.bubble .oli{padding-left:18px;margin:3px 0;display:block}
.bubble .hr{border:none;border-top:1px solid rgba(255,255,255,0.07);margin:10px 0;display:block}
.bubble .cb{
  background:rgba(0,0,0,0.45);border-radius:10px;
  padding:12px 14px;margin:9px 0;
  font-family:'JetBrains Mono','Fira Code',monospace;
  font-size:12.5px;overflow-x:auto;
  color:#a5f3fc;border:1px solid rgba(255,255,255,0.06);display:block;
}
.bubble .ic{
  background:rgba(56,189,248,0.1);padding:1px 6px;border-radius:5px;
  font-family:'JetBrains Mono',monospace;font-size:12px;color:#7dd3fc;
}

.dots{display:flex;gap:5px;align-items:center;padding:4px 0}
.dots span{width:7px;height:7px;border-radius:50%;background:#334155;animation:blink 1.2s infinite}
.dots span:nth-child(2){animation-delay:.22s}
.dots span:nth-child(3){animation-delay:.44s}
@keyframes blink{0%,80%,100%{opacity:.2;transform:scale(.75)}40%{opacity:1;transform:scale(1)}}

.tool-row{display:flex;flex-wrap:wrap;gap:6px;padding:2px 0 6px 38px}
.tool-pill{
  display:inline-flex;align-items:center;gap:6px;
  padding:4px 10px;border-radius:20px;
  font-size:11px;font-weight:600;transition:all .3s;
}
.tactive{background:rgba(56,189,248,0.07);border:1px solid rgba(56,189,248,0.18);color:#7dd3fc}
.tdone{background:rgba(34,197,94,0.06);border:1px solid rgba(34,197,94,0.13);color:#86efac}
.tdot{width:5px;height:5px;border-radius:50%;background:currentColor;animation:tp 1.1s ease-in-out infinite}
.tdone .tdot{animation:none}
.tick{color:#4ade80;font-size:10px}
@keyframes tp{0%,100%{opacity:.35}50%{opacity:1}}

.input-area{
  padding:8px 16px 18px;
  max-width:740px;width:100%;margin:0 auto;
  position:relative;z-index:10;flex-shrink:0;
}
.input-box{
  border-radius:18px;
  border:1px solid rgba(255,255,255,0.07);
  background:rgba(255,255,255,0.025);
  backdrop-filter:blur(24px);
  transition:border-color .3s;overflow:hidden;
}
.input-box:focus-within{border-color:rgba(255,255,255,0.12)}

.slash-menu{
  background:#0c1525;
  border-bottom:1px solid rgba(56,189,248,0.12);
  max-height:200px;overflow-y:auto;
}
.slash-item{
  display:flex;align-items:center;gap:12px;
  padding:9px 14px;transition:background .15s;
}
.slash-item:hover{background:rgba(56,189,248,0.08)}
.slash-cmd{font-weight:700;color:#7dd3fc;font-size:12.5px;font-family:monospace}
.slash-desc{color:#64748b;font-size:12px}

.input-inner{display:flex;flex-direction:column;padding:8px 10px 8px 10px}
.inp-row{display:flex;align-items:flex-end;gap:6px}

/* File chips */
.file-chips{display:flex;flex-wrap:wrap;gap:6px;padding:6px 4px 2px}
.file-chip{
  display:flex;align-items:center;gap:5px;
  background:rgba(56,189,248,0.1);border:1px solid rgba(56,189,248,0.2);
  border-radius:8px;padding:3px 8px;font-size:11px;color:#7dd3fc;max-width:180px;
}
.file-chip-icon{font-size:12px;flex-shrink:0}
.file-chip-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-chip-rm{
  background:none;border:none;color:#475569;padding:0 0 0 2px;
  font-size:14px;line-height:1;flex-shrink:0;
}
.file-chip-rm:hover{color:#fb7185}

/* Attach button */
.attach-btn{color:#475569;transition:color .2s;flex-shrink:0}
.attach-btn:hover{color:var(--c)}

.inp{
  flex:1;background:transparent;border:none;color:#f1f5f9;
  font-size:14px;resize:none;line-height:1.55;
  min-height:24px;max-height:120px;padding:2px 0;overflow-y:hidden;
}
.inp::placeholder{color:#2d3f55}
.inp:focus{outline:none}

.inp-btns{display:flex;align-items:center;gap:6px;flex-shrink:0}
.mic-btn{color:#475569;transition:color .2s}
.mic-btn.mic-on{color:#fb7185 !important}

.send-btn{
  width:34px;height:34px;border-radius:10px;border:none;
  color:#fff;display:flex;align-items:center;justify-content:center;
  transition:all .2s;flex-shrink:0;
}
.send-btn:disabled{opacity:.45}
.send-btn:not(:disabled):hover{transform:translateY(-1px);filter:brightness(1.15)}

.footer-lbl{
  text-align:center;color:#1a2a3a;
  font-size:10px;margin-top:7px;letter-spacing:.4px;
}

/* Notifications */
.notif-btn{position:relative}
.notif-badge{
  position:absolute;top:-3px;right:-3px;
  background:#fb7185;color:#fff;font-size:9px;font-weight:700;
  width:14px;height:14px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  border:1.5px solid #070c18;
}
.notif-drop{
  position:absolute;right:0;top:calc(100% + 8px);width:280px;
  background:#0f172a;border:1px solid rgba(255,255,255,0.08);
  border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.6);
  z-index:100;overflow:hidden;
}
.notif-head{padding:12px 14px 8px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:.5px;text-transform:uppercase}
.notif-empty{padding:12px 14px 14px;font-size:13px;color:#334155;text-align:center}
.notif-item{
  display:flex;align-items:flex-start;gap:10px;
  padding:10px 14px;border-top:1px solid rgba(255,255,255,0.04);
}
.notif-icon{font-size:14px;flex-shrink:0;margin-top:1px}
.notif-txt{font-size:12px;color:#cbd5e1;line-height:1.4}
.notif-time{font-size:10px;color:#334155;margin-top:3px}

/* Contacts modal */
.modal-search{
  width:100%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);
  border-radius:8px;padding:8px 12px;color:#e2e8f0;font-size:13px;margin-bottom:8px;
}
.modal-search::placeholder{color:#334155}
.modal-search:focus{outline:none;border-color:rgba(56,189,248,0.3)}
.contact-item{display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.04)}
.contact-avatar{
  width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,#38bdf8,#6366f1);
  display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;
  color:#fff;flex-shrink:0;
}
.contact-info{flex:1;min-width:0}
.contact-name{font-size:13px;font-weight:600;color:#e2e8f0}
.contact-co{font-weight:400;color:#64748b;font-size:12px}
.contact-detail{font-size:11px;color:#475569;margin-top:2px}
.contact-notes{font-size:11px;color:#334155;margin-top:4px;font-style:italic;line-height:1.4}

/* Generated images */
.gen-img-wrap{margin-top:10px;border-radius:10px;overflow:hidden;border:1px solid rgba(255,255,255,0.08)}
.gen-img{width:100%;max-width:400px;display:block;border-radius:10px}
.img-dl{
  display:inline-block;margin:6px 8px 4px;font-size:11px;color:#7dd3fc;
  text-decoration:none;opacity:.8;
}
.img-dl:hover{opacity:1}

/* Export PDF button */
.export-btn{
  display:inline-flex;align-items:center;gap:4px;
  margin-top:8px;padding:4px 10px;border-radius:7px;border:1px solid rgba(255,255,255,0.08);
  background:transparent;color:#475569;font-size:11px;
  transition:all .2s;
}
.export-btn:hover{color:#7dd3fc;border-color:rgba(125,211,252,0.3);background:rgba(125,211,252,0.06)}

.overlay-bg{position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:40;backdrop-filter:blur(2px)}
.sidebar{
  position:fixed;left:0;top:0;bottom:0;width:280px;
  background:#08111e;
  border-right:1px solid rgba(255,255,255,0.05);
  z-index:50;display:flex;flex-direction:column;
  animation:slin .2s ease;
}
@keyframes slin{from{transform:translateX(-100%)}to{transform:translateX(0)}}
.sb-head{padding:16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid rgba(255,255,255,0.05)}
.sb-title{font-weight:700;font-size:14px;color:#e2e8f0}
.sb-btn{
  padding:4px 10px;border-radius:8px;
  border:1px solid rgba(255,255,255,0.08);
  background:rgba(255,255,255,0.04);color:#94a3b8;
  font-size:11px;font-weight:600;transition:all .18s;
}
.sb-btn:hover{background:rgba(255,255,255,0.09);color:#e2e8f0}
.sb-list{flex:1;overflow-y:auto;padding:8px}
.sb-empty{color:#364151;text-align:center;padding:28px 16px;font-size:13px}
.sb-item{padding:10px 12px;border-radius:10px;margin-bottom:3px;transition:background .15s;position:relative}
.sb-item:hover,.sb-item.active{background:rgba(56,189,248,0.06)}
.sb-item.active{border-left:2px solid var(--c,#38bdf8)}
.sb-item-name{font-size:13px;font-weight:600;color:#e2e8f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:22px}
.sb-item-meta{font-size:10px;color:#475569;margin-top:2px}
.sb-del{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:#2d3f55;font-size:11px;transition:color .15s}
.sb-del:hover{color:#f87171}

.overlay{position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(4px)}
.modal{
  background:#0b1423;border:1px solid rgba(255,255,255,0.06);
  border-radius:20px;padding:20px;
  width:100%;max-width:480px;max-height:80vh;overflow-y:auto;
  animation:modin .2s ease;
}
@keyframes modin{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px}
.modal-title{font-weight:800;font-size:18px;color:#f8fafc}
.modal-sub{color:#38bdf8;font-size:11px;margin-top:3px}
.modal-empty{color:#4b5563;text-align:center;padding:40px 16px;font-size:14px}
.mem-list{display:flex;flex-direction:column;gap:6px}
.mem-item{background:rgba(56,189,248,0.045);border:1px solid rgba(56,189,248,0.1);border-radius:10px;padding:10px 13px}
.mem-k{color:#7dd3fc;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px}
.mem-v{color:#d1d5db;font-size:13px;margin-top:4px;line-height:1.5}
.mem-cat{display:inline-block;font-size:9px;color:#64748b;background:rgba(255,255,255,0.05);padding:2px 7px;border-radius:6px;margin-top:5px}
.btn-danger{margin-top:16px;width:100%;padding:10px;border-radius:10px;border:1px solid rgba(239,68,68,0.18);background:rgba(239,68,68,0.05);color:#f87171;font-size:12px;font-weight:600;transition:all .2s}
.btn-danger:hover{background:rgba(239,68,68,0.1)}

@media(max-width:640px){
  .mtog-wrap{display:none}
  .h-pill{padding:4px 9px;font-size:10px}
  .msgs{padding:14px 14px 22px}
  .welcome-txt{font-size:18px}
}
`;
