import { Suspense, lazy, useEffect, useState } from 'react';
import {
  Activity, AlertTriangle, AppWindow, BarChart3, Bot, CalendarClock, ChevronLeft, ChevronRight,
  Clapperboard, Clock3, Cpu, Crosshair, FileBarChart, FileText, FileVideo, FlaskConical,
  FolderKanban, GitCompareArrows, HardDrive, Hash, History, KeyRound, LayoutDashboard, LayoutGrid,
  LibraryBig, LineChart, Link2, ListChecks, LoaderCircle, LockKeyhole, MessageSquare, Moon, Network,
  PenLine, Play, ScrollText, Send, Settings as SettingsIcon, ShieldCheck, Sliders, SlidersHorizontal, Sparkles,
  Sun, TrendingUp, Truck, Upload, UserRound, UsersRound, Workflow, Wrench,
  type LucideIcon,
} from 'lucide-react';
import { CreatorApi } from './api';
import { LibraryApi } from './apiLibrary';
import { PublishingApi } from './apiPublishing';
import { SocialApi } from './apiSocial';
import { AnalyticsApi } from './services/apiAnalytics';
import { EmptyState } from './components/ui';
const AnalyticsOverview = lazy(() => import('./pages/analytics/Overview'));
const PlatformComparison = lazy(() => import('./pages/analytics/PlatformComparison'));
const AccountPerformance = lazy(() => import('./pages/analytics/AccountPerformance'));
const PublicationDetail = lazy(() => import('./pages/analytics/PublicationDetail'));
const ContentComparison = lazy(() => import('./pages/analytics/ContentComparison'));
const CampaignAnalytics = lazy(() => import('./pages/analytics/CampaignAnalytics'));
const VideoPerformance = lazy(() => import('./pages/analytics/VideoPerformance'));
const AudienceGrowth = lazy(() => import('./pages/analytics/AudienceGrowth'));
const HashtagAnalysis = lazy(() => import('./pages/analytics/HashtagAnalysis'));
const Recommendations = lazy(() => import('./pages/analytics/Recommendations'));
const Reports = lazy(() => import('./pages/analytics/Reports'));
const AnalyticsSettings = lazy(() => import('./pages/analytics/AnalyticsSettings'));
import AccountDetailPage from './pages/social/AccountDetailPage';
import AppSettings from './pages/social/AppSettings';
import ConnectWizard from './pages/social/ConnectWizard';
import Permissions from './pages/social/Permissions';
import SocialOverview from './pages/social/SocialOverview';
import VaultStatus from './pages/social/VaultStatus';
import ApprovalQueue from './pages/publishing/ApprovalQueue';
import AttentionList from './pages/publishing/AttentionList';
import Composer from './pages/publishing/Composer';
import DeliverySettings from './pages/publishing/DeliverySettings';
import DestinationDetail from './pages/publishing/DestinationDetail';
import IntentDetail from './pages/publishing/IntentDetail';
import PublishQueue from './pages/publishing/PublishQueue';
import PublishedHistory from './pages/publishing/PublishedHistory';
import PublishingOverview from './pages/publishing/PublishingOverview';
import ScheduleCenter from './pages/publishing/ScheduleCenter';
import { WorkforceApi } from './apiWorkforce';
import Dashboard from './pages/Dashboard';
import Setup from './pages/Setup';
import Infrastructure from './pages/Infrastructure';
import ImportManager from './pages/library/ImportManager';
import LibraryOverview from './pages/library/LibraryOverview';
import MediaDetail from './pages/library/MediaDetail';
import MediaGrid from './pages/library/MediaGrid';
import ProcessingCenter from './pages/library/ProcessingCenter';
import StorageManagement from './pages/library/StorageManagement';
import TranscodeStudio from './pages/library/TranscodeStudio';
import ModelLab from './pages/ModelLab';
import Playground from './pages/Playground';
import Settings from './pages/Settings';
import ActivityLog from './pages/workforce/ActivityLog';
import AgentDetail from './pages/workforce/AgentDetail';
import AgentPlayground from './pages/workforce/AgentPlayground';
import AgentSettings from './pages/workforce/AgentSettings';
import Directory from './pages/workforce/Directory';
import LiveMonitor from './pages/workforce/LiveMonitor';
import Organization from './pages/workforce/Organization';
import TaskGraph from './pages/workforce/TaskGraph';
import WorkforceOverview from './pages/workforce/WorkforceOverview';

type Diagnostics = {
  app: { name: string; version: string; uptime_s: number };
  runtime: { python: string; executable: string; pid: number };
  system: { cpu_percent: number; ram_total_gb: number; ram_available_gb: number };
  disk: { root: string; free_gb: number; total_gb: number; percent_used: number; ok: boolean };
  paths: Record<string, string>;
  ffmpeg: { present: boolean; path: string; version?: string; error?: string };
  ollama: { online: boolean; version?: string; models?: string[]; model_count?: number; error?: string };
};

type Bootstrap = { apiBase: string; token: string; ready: boolean; error: string | null; root: string; isDev: boolean };

type VersionInfo = { name: string; version: string; api: string; capabilities: Record<string, string> };


declare global {
  interface Window {
    creatoros?: {
      bootstrap: () => Promise<Bootstrap>;
      backendLog: () => Promise<string[]>;
      openPath: (target: string) => Promise<{ ok: boolean; error?: string }>;
      pickMedia?: () => Promise<{ canceled: boolean; files: { path: string; name: string; size_bytes: number }[] }>;
      importMedia?: (paths: string[]) => Promise<{
        ok: boolean;
        status?: number;
        error?: string;
        payload?: { jobs: unknown[]; duplicates: unknown[]; rejected: unknown[] };
      }>;
      vaultStore?: (request: {
        id: string;
        secret: string;
        purpose: string;
        provider?: string;
        alias?: string;
      }) => Promise<{
        ok: boolean;
        code?: string;
        error?: string;
        record?: { id: string; alg: string; bytes: number; fingerprint: string; version: number };
      }>;
      vaultHealth?: () => Promise<{ available: boolean; backend: string; protection: string; entries?: number; reason?: string }>;
      vaultDelete?: (id: string) => Promise<{ ok: boolean; removed?: boolean; code?: string; error?: string }>;
      openAuthUrl?: (url: string) => Promise<{ ok: boolean; error?: string }>;
    };
  }
}

const NAV = [
  { id: 'overview', label: 'Overview', section: 'Studio' },
  { id: 'setup', label: 'First-run setup', section: 'Studio' },
  { id: 'ai', label: 'AI infrastructure', section: 'Studio' },
  { id: 'lab', label: 'Model Lab', section: 'Studio' },
  { id: 'playground', label: 'Playground', section: 'Studio' },
  { id: 'settings', label: 'Settings', section: 'Studio' },
  { id: 'lib-overview', label: 'Overview', section: 'Content library' },
  { id: 'lib-grid', label: 'Media grid', section: 'Content library' },
  { id: 'lib-import', label: 'Import manager', section: 'Content library' },
  { id: 'lib-detail', label: 'Media detail', section: 'Content library' },
  { id: 'lib-processing', label: 'Processing centre', section: 'Content library' },
  { id: 'lib-transcode', label: 'Transcode studio', section: 'Content library' },
  { id: 'lib-storage', label: 'Storage', section: 'Content library' },
  { id: 'wf-overview', label: 'Overview', section: 'Agent workforce' },
  { id: 'wf-org', label: 'Organization', section: 'Agent workforce' },
  { id: 'wf-directory', label: 'Directory', section: 'Agent workforce' },
  { id: 'wf-agent', label: 'Agent detail', section: 'Agent workforce' },
  { id: 'wf-graph', label: 'Task graph', section: 'Agent workforce' },
  { id: 'wf-monitor', label: 'Live monitor', section: 'Agent workforce' },
  { id: 'wf-activity', label: 'Activity log', section: 'Agent workforce' },
  { id: 'wf-playground', label: 'Playground', section: 'Agent workforce' },
  { id: 'wf-settings', label: 'Settings', section: 'Agent workforce' },
  { id: 'soc-overview', label: 'Connected accounts', section: 'Social accounts' },
  { id: 'soc-connect', label: 'Connect account', section: 'Social accounts' },
  { id: 'soc-account', label: 'Account details', section: 'Social accounts' },
  { id: 'soc-permissions', label: 'Permissions', section: 'Social accounts' },
  { id: 'soc-vault', label: 'Vault status', section: 'Social accounts' },
  { id: 'soc-apps', label: 'App settings', section: 'Social accounts' },
  { id: 'pub-overview', label: 'Publishing', section: 'Publishing' },
  { id: 'pub-compose', label: 'Compose', section: 'Publishing' },
  { id: 'pub-intent', label: 'Intent detail', section: 'Publishing' },
  { id: 'pub-approvals', label: 'Approvals', section: 'Publishing' },
  { id: 'pub-queue', label: 'Publish queue', section: 'Publishing' },
  { id: 'pub-schedule', label: 'Schedule', section: 'Publishing' },
  { id: 'pub-published', label: 'Published', section: 'Publishing' },
  { id: 'pub-destination', label: 'Destination detail', section: 'Publishing' },
  { id: 'pub-attention', label: 'Needs you', section: 'Publishing' },
  { id: 'pub-delivery', label: 'Media delivery', section: 'Publishing' },
  { id: 'an-overview', label: 'Overview', section: 'Analytics' },
  { id: 'an-compare', label: 'Platform comparison', section: 'Analytics' },
  { id: 'an-account', label: 'Account performance', section: 'Analytics' },
  { id: 'an-publication', label: 'Publication detail', section: 'Analytics' },
  { id: 'an-content', label: 'Content comparison', section: 'Analytics' },
  { id: 'an-campaigns', label: 'Campaign analytics', section: 'Analytics' },
  { id: 'an-video', label: 'Video performance', section: 'Analytics' },
  { id: 'an-audience', label: 'Audience growth', section: 'Analytics' },
  { id: 'an-hashtags', label: 'Hashtag analysis', section: 'Analytics' },
  { id: 'an-recommendations', label: 'Recommendations', section: 'Analytics' },
  { id: 'an-reports', label: 'Reports', section: 'Analytics' },
  { id: 'an-settings', label: 'Analytics settings', section: 'Analytics' },
] as const;

type PageId = (typeof NAV)[number]['id'];

const NAV_ICONS: Record<PageId, LucideIcon> = {
  overview: LayoutDashboard,
  setup: Wrench,
  ai: Cpu,
  lab: FlaskConical,
  playground: Play,
  settings: SettingsIcon,
  'lib-overview': LibraryBig,
  'lib-grid': LayoutGrid,
  'lib-import': Upload,
  'lib-detail': FileVideo,
  'lib-processing': LoaderCircle,
  'lib-transcode': SlidersHorizontal,
  'lib-storage': HardDrive,
  'wf-overview': Bot,
  'wf-org': Network,
  'wf-directory': UsersRound,
  'wf-agent': UserRound,
  'wf-graph': Workflow,
  'wf-monitor': Activity,
  'wf-activity': ScrollText,
  'wf-playground': MessageSquare,
  'wf-settings': SettingsIcon,
  'soc-overview': ShieldCheck,
  'soc-connect': Link2,
  'soc-account': UserRound,
  'soc-permissions': KeyRound,
  'soc-vault': LockKeyhole,
  'soc-apps': AppWindow,
  'pub-overview': Send,
  'pub-compose': PenLine,
  'pub-intent': FileText,
  'pub-approvals': ListChecks,
  'pub-queue': Clock3,
  'pub-schedule': CalendarClock,
  'pub-published': History,
  'pub-destination': Crosshair,
  'pub-attention': AlertTriangle,
  'pub-delivery': Truck,
  'an-overview': BarChart3,
  'an-compare': GitCompareArrows,
  'an-account': LineChart,
  'an-publication': FileText,
  'an-content': LayoutGrid,
  'an-campaigns': FolderKanban,
  'an-video': Clapperboard,
  'an-audience': TrendingUp,
  'an-hashtags': Hash,
  'an-recommendations': Sparkles,
  'an-reports': FileBarChart,
  'an-settings': Sliders,
};

export default function App() {
  const [page, setPage] = useState<PageId>('overview');
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [api, setApi] = useState<CreatorApi | null>(null);
  const [wf, setWf] = useState<WorkforceApi | null>(null);
  const [lib, setLib] = useState<LibraryApi | null>(null);
  const [social, setSocial] = useState<SocialApi | null>(null);
  const [publishing, setPublishing] = useState<PublishingApi | null>(null);
  const [analytics, setAnalytics] = useState<AnalyticsApi | null>(null);
  const [analyticsPublication, setAnalyticsPublication] = useState('');
  const [selectedIntent, setSelectedIntent] = useState<string | null>(null);
  const [selectedVariant, setSelectedVariant] = useState<string | null>(null);
  const [selectedAccount, setSelectedAccount] = useState<string | null>(null);
  const [selectedAsset, setSelectedAsset] = useState<string | null>(null);
  const [focusedJob, setFocusedJob] = useState<string | null>(null);
  const [selectedAgent, setSelectedAgent] = useState('chief_orchestrator');
  const [selectedObjective, setSelectedObjective] = useState<string | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [version, setVersion] = useState<VersionInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        if (!window.creatoros) throw new Error('Preload bridge unavailable - this page must run inside the CreatorOS shell.');
        const b = await window.creatoros.bootstrap();
        if (cancelled) return;
        setBoot(b);
        if (!b.ready) {
          setError(b.error ?? 'Backend is not ready.');
          setLog(await window.creatoros.backendLog());
          setLoading(false);
          return;
        }
        setApi(new CreatorApi(b.apiBase, b.token));
        setWf(new WorkforceApi(b.apiBase, b.token));
        setLib(new LibraryApi(b.apiBase, b.token));
        setSocial(new SocialApi(b.apiBase, b.token));
        setPublishing(new PublishingApi(b.apiBase, b.token));
        setAnalytics(new AnalyticsApi(b.apiBase, b.token));
        const auth = { Authorization: `Bearer ${b.token}` };
        const [diagRes, versionRes, setupRes] = await Promise.all([
          fetch(`${b.apiBase}/diagnostics`, { headers: auth }),
          fetch(`${b.apiBase}/version`, { headers: auth }),
          fetch(`${b.apiBase}/setup/status`, { headers: auth }).catch(() => null),
        ]);
        if (!diagRes.ok) throw new Error(`Diagnostics request failed: HTTP ${diagRes.status}`);
        const [diagData, versionData, setupData] = (await Promise.all([
          diagRes.json(),
          versionRes.ok ? versionRes.json() : null,
          setupRes && setupRes.ok ? setupRes.json() : null,
        ])) as [Diagnostics, VersionInfo | null, { setup_complete?: boolean } | null];
        if (!cancelled) {
          setDiag(diagData);
          setVersion(versionData);
          // A machine that has never finished the wizard lands on it; "Not now" leaves the user on
          // the overview and the page stays one click away in the rail.
          if (setupData?.setup_complete === false) setPage('setup');
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const aiOnline = diag?.ollama.online ?? false;

  const sections = ['Studio', 'Content library', 'Agent workforce', 'Social accounts', 'Publishing', 'Analytics'] as const;
  const current = NAV.find((item) => item.id === page);
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    const stored = localStorage.getItem('creatoros.theme');
    if (stored === 'dark' || stored === 'light') return stored;
    return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  });
  const [railCollapsed, setRailCollapsed] = useState(() => localStorage.getItem('creatoros.rail.collapsed') === '1');

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('creatoros.theme', theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem('creatoros.rail.collapsed', railCollapsed ? '1' : '0');
  }, [railCollapsed]);

  return (
    <div className={`app-shell${railCollapsed ? ' app-shell--collapsed' : ''}`}>
      <aside className="rail">
        <div className="rail__brand">
          <span className="rail__mark" aria-hidden="true">C</span>
          <div className="rail__brand-text">
            <div className="rail__title">CreatorOS</div>
            <div className="rail__tagline">Your AI-powered content studio.</div>
          </div>
          <button
            type="button"
            className="rail__collapse"
            onClick={() => setRailCollapsed(!railCollapsed)}
            aria-label={railCollapsed ? 'Expand navigation' : 'Collapse navigation'}
            title={railCollapsed ? 'Expand navigation' : 'Collapse navigation'}
          >
            {railCollapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
          </button>
        </div>
        <nav className="rail__scroll" aria-label="Primary navigation">
          {sections.map((section) => (
            <div key={section} className="rail__section">
              <p className="rail__label">{section}</p>
              {NAV.filter((item) => item.section === section).map((item) => {
                const ItemIcon = NAV_ICONS[item.id];
                return (
                  <button
                    key={item.id}
                    type="button"
                    className="rail__item"
                    aria-current={page === item.id ? 'page' : undefined}
                    onClick={() => setPage(item.id)}
                    title={railCollapsed ? item.label : undefined}
                  >
                    <ItemIcon className="rail__icon" size={16} strokeWidth={1.8} aria-hidden="true" />
                    <span className="rail__label-text">{item.label}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="rail__footer">
          <strong>{version ? `v${version.version}` : 'version pending'}</strong>
          <span>Local data · runs on this machine</span>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <div className="topbar__crumb">
            {current && current.section !== current.label ? (
              <span className="topbar__section">{current.section} /</span>
            ) : null}
            <h1 className="topbar__title">{current?.label ?? 'Overview'}</h1>
          </div>
          <div className="topbar__status">
            <span className={`status ${aiOnline ? 'status--ok' : 'status--warn'}`}>
              <span className="status__dot" aria-hidden="true" />
              {aiOnline ? `Ollama ${diag?.ollama.version ?? ''}` : 'Ollama offline'}
            </span>
            <span className={`status ${boot?.ready ? 'status--ok' : 'status--bad'}`}>
              <span className="status__dot" aria-hidden="true" />
              {boot?.ready ? 'Backend ready' : 'Backend offline'}
            </span>
            <button
              type="button"
              className="theme-toggle"
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            >
              {theme === 'dark' ? <Sun size={13} aria-hidden="true" /> : <Moon size={13} aria-hidden="true" />}
              {theme === 'dark' ? 'Light' : 'Dark'}
            </button>
          </div>
        </header>

        <main className="app-content">
          {loading && !diag ? (
            <section className="rounded-xl border border-slate-800 bg-slate-900 p-5 text-sm text-slate-400">
              Loading live diagnostics…
            </section>
          ) : null}

          {error && (
            <section className="rounded-xl border border-rose-400/30 bg-slate-900 p-5">
              <h2 className="text-[13px] font-semibold text-slate-200">Backend unavailable</h2>
              <p className="mt-2 text-sm text-rose-400">{error}</p>
              <p className="mt-2 text-xs text-slate-500">Nothing here is simulated. Fix the backend and reload.</p>
              <pre className="mt-3 max-h-64 overflow-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-400">
                {log.join('\n') || 'no backend log captured'}
              </pre>
            </section>
          )}

          {api && page === 'overview' && (
            <Dashboard
              api={api}
              version={version}
              library={lib}
              publishing={publishing}
              social={social}
              onNavigate={(target) => setPage(target as PageId)}
            />
          )}

          {api && page === 'setup' && <Setup api={api} onDone={() => setPage('overview')} />}

          {lib && page === 'lib-overview' && (
            <LibraryOverview
              library={lib}
              onNavigate={(target) => setPage(target as PageId)}
              onOpenAsset={(id) => {
                setSelectedAsset(id);
                setPage('lib-detail');
              }}
            />
          )}
          {lib && page === 'lib-grid' && (
            <MediaGrid
              library={lib}
              onOpenAsset={(id) => {
                setSelectedAsset(id);
                setPage('lib-detail');
              }}
              onImport={() => setPage('lib-import')}
            />
          )}
          {lib && page === 'lib-import' && (
            <ImportManager
              library={lib}
              onOpenAsset={(id) => {
                setSelectedAsset(id);
                setPage('lib-detail');
              }}
            />
          )}
          {lib && page === 'lib-detail' && selectedAsset && (
            <MediaDetail
              library={lib}
              assetId={selectedAsset}
              onBack={() => setPage('lib-grid')}
              onOpenJob={(jobId) => {
                setFocusedJob(jobId);
                setPage('lib-processing');
              }}
            />
          )}
          {lib && page === 'lib-detail' && !selectedAsset && (
            <MediaGrid
              library={lib}
              onOpenAsset={(id) => setSelectedAsset(id)}
              onImport={() => setPage('lib-import')}
            />
          )}
          {lib && page === 'lib-processing' && <ProcessingCenter library={lib} focusJobId={focusedJob} onOpenAsset={(id) => {
            setSelectedAsset(id);
            setPage('lib-detail');
          }} />}
          {lib && page === 'lib-transcode' && (
            <TranscodeStudio
              library={lib}
              onOpenAsset={(id) => {
                setSelectedAsset(id);
                setPage('lib-detail');
              }}
            />
          )}
          {lib && page === 'lib-storage' && (
            <StorageManagement
              library={lib}
              onOpenAsset={(id) => {
                setSelectedAsset(id);
                setPage('lib-detail');
              }}
            />
          )}

          {social && page === 'soc-overview' && (
            <SocialOverview
              api={social}
              onOpenAccount={(id) => {
                setSelectedAccount(id);
                setPage('soc-account');
              }}
              onConnect={() => setPage('soc-connect')}
            />
          )}
          {social && page === 'soc-connect' && (
            <ConnectWizard
              api={social}
              onConnected={(id) => {
                if (id) setSelectedAccount(id);
              }}
              onOpenAppSettings={() => setPage('soc-apps')}
            />
          )}
          {social && page === 'soc-account' && selectedAccount && (
            <AccountDetailPage
              api={social}
              accountId={selectedAccount}
              onBack={() => setPage('soc-overview')}
              onConnect={() => setPage('soc-connect')}
            />
          )}
          {social && page === 'soc-account' && !selectedAccount && (
            <SocialOverview
              api={social}
              onOpenAccount={(id) => setSelectedAccount(id)}
              onConnect={() => setPage('soc-connect')}
            />
          )}
          {social && page === 'soc-permissions' && (
            <Permissions
              api={social}
              analytics={analytics ?? undefined}
              onOpenAccount={(id) => {
                setSelectedAccount(id);
                setPage('soc-account');
              }}
            />
          )}
          {social && page === 'soc-vault' && (
            <VaultStatus
              api={social}
              onOpenAccount={(id) => {
                setSelectedAccount(id);
                setPage('soc-account');
              }}
            />
          )}
          {social && page === 'soc-apps' && <AppSettings api={social} onConnect={() => setPage('soc-connect')} />}

          {publishing && page === 'pub-overview' && (
            <PublishingOverview
              api={publishing}
              onOpenComposer={() => setPage('pub-compose')}
              onOpenQueue={() => setPage('pub-queue')}
              onOpenDelivery={() => setPage('pub-delivery')}
              onOpenAttention={() => setPage('pub-attention')}
            />
          )}
          {publishing && page === 'pub-compose' && (
            <Composer
              api={publishing}
              onOpenIntent={(id) => {
                setSelectedIntent(id);
                setPage('pub-intent');
              }}
            />
          )}
          {publishing && page === 'pub-intent' && selectedIntent && (
            <IntentDetail
              api={publishing}
              projectId={selectedIntent}
              onOpenComposer={() => setPage('pub-compose')}
              onOpenDestination={(id) => {
                setSelectedVariant(id);
                setPage('pub-destination');
              }}
            />
          )}
          {publishing && page === 'pub-intent' && !selectedIntent && (
            <Composer
              api={publishing}
              onOpenIntent={(id) => {
                setSelectedIntent(id);
                setPage('pub-intent');
              }}
            />
          )}
          {publishing && page === 'pub-approvals' && (
            <ApprovalQueue
              api={publishing}
              onOpenDestination={(id) => {
                setSelectedVariant(id);
                setPage('pub-destination');
              }}
            />
          )}
          {publishing && page === 'pub-queue' && (
            <PublishQueue
              api={publishing}
              onOpenDestination={(id) => {
                setSelectedVariant(id);
                setPage('pub-destination');
              }}
            />
          )}
          {publishing && page === 'pub-schedule' && (
            <ScheduleCenter
              api={publishing}
              onOpenDestination={(id) => {
                setSelectedVariant(id);
                setPage('pub-destination');
              }}
            />
          )}
          {publishing && page === 'pub-published' && (
            <PublishedHistory
              api={publishing}
              onOpenDestination={(id) => {
                setSelectedVariant(id);
                setPage('pub-destination');
              }}
            />
          )}
          {publishing && page === 'pub-destination' && selectedVariant && (
            <DestinationDetail api={publishing} variantId={selectedVariant} onBack={() => setPage('pub-queue')} />
          )}
          {publishing && page === 'pub-destination' && !selectedVariant && (
            <EmptyState title="No destination selected" hint="Open one from the queue, the schedule or a published post." />
          )}
          {publishing && page === 'pub-attention' && (
            <AttentionList
              api={publishing}
              onOpenDestination={(id) => {
                setSelectedVariant(id);
                setPage('pub-destination');
              }}
            />
          )}
          {publishing && page === 'pub-delivery' && <DeliverySettings api={publishing} />}

          {api && page === 'ai' && <Infrastructure api={api} version={version} />}
          {api && page === 'lab' && <ModelLab api={api} />}
          {api && page === 'playground' && <Playground api={api} disabled={!aiOnline} />}
          {api && page === 'settings' && <Settings api={api} />}

          {wf && page === 'wf-overview' && (
            <WorkforceOverview
              wf={wf}
              onOpenObjective={(id) => {
                setSelectedObjective(id);
                setPage('wf-graph');
              }}
            />
          )}
          {wf && page === 'wf-org' && (
            <Organization
              wf={wf}
              onOpenAgent={(id) => {
                setSelectedAgent(id);
                setPage('wf-agent');
              }}
            />
          )}
          {wf && page === 'wf-directory' && (
            <Directory
              wf={wf}
              onOpenAgent={(id) => {
                setSelectedAgent(id);
                setPage('wf-agent');
              }}
            />
          )}
          {wf && page === 'wf-agent' && (
            <AgentDetail wf={wf} agentId={selectedAgent} onBack={() => setPage('wf-directory')} />
          )}
          {wf && page === 'wf-graph' && (
            <TaskGraph
              wf={wf}
              objectiveId={selectedObjective}
              onOpenObjective={(id) => setSelectedObjective(id)}
            />
          )}
          {wf && page === 'wf-monitor' && (
            <LiveMonitor
              wf={wf}
              onOpenObjective={(id) => {
                setSelectedObjective(id);
                setPage('wf-graph');
              }}
            />
          )}
          {wf && page === 'wf-activity' && <ActivityLog wf={wf} />}
          {wf && page === 'wf-playground' && (
            <AgentPlayground
              wf={wf}
              onOpenObjective={(id) => {
                setSelectedObjective(id);
                setPage('wf-graph');
              }}
            />
          )}
          {wf && page === 'wf-settings' && <AgentSettings wf={wf} />}

          <Suspense fallback={<div className="panel">Loading analytics…</div>}>
          {analytics && page === 'an-overview' && <AnalyticsOverview api={analytics} />}
          {analytics && page === 'an-compare' && <PlatformComparison api={analytics} />}
          {analytics && page === 'an-account' && <AccountPerformance api={analytics} />}
          {analytics && page === 'an-publication' && <PublicationDetail api={analytics} initialPublicationId={analyticsPublication} />}
          {analytics && page === 'an-content' && <ContentComparison api={analytics} />}
          {analytics && page === 'an-campaigns' && <CampaignAnalytics api={analytics} />}
          {analytics && page === 'an-video' && (
            <VideoPerformance
              api={analytics}
              onOpenPublication={(id) => {
                setAnalyticsPublication(id);
                setPage('an-publication');
              }}
            />
          )}
          {analytics && page === 'an-audience' && <AudienceGrowth api={analytics} />}
          {analytics && page === 'an-hashtags' && (
            <HashtagAnalysis
              api={analytics}
              onOpenPublication={(id) => {
                setAnalyticsPublication(id);
                setPage('an-publication');
              }}
            />
          )}
          {analytics && page === 'an-recommendations' && <Recommendations api={analytics} />}
          {analytics && page === 'an-reports' && <Reports api={analytics} />}
          {analytics && page === 'an-settings' && <AnalyticsSettings api={analytics} />}
          </Suspense>
        </main>
      </div>
    </div>
  );
}