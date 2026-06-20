import { useEffect, useMemo, useRef, useState } from "react";

const DATA_URL = "/data/hotemin_posts.json";
const SONG_URL = "/assets/song.mp3";
const SORT_OPTIONS = [
  { key: "score", label: "Score" },
  { key: "tweet_count", label: "Posts" },
  { key: "views", label: "Views" },
  { key: "likes", label: "Likes" },
  { key: "engagement", label: "Engagement" },
];

const metricLabels = {
  tweet_count: "Posts",
  likes: "Likes",
  views: "Views",
  replies: "Replies",
  retweets: "Retweets",
  quotes: "Quotes",
  bookmarks: "Bookmarks",
};

function numberFormat(value) {
  return new Intl.NumberFormat("en", {
    notation: value >= 100000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(Number(value || 0));
}

function dateLabel(value) {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Unknown";
  return parsed.toLocaleDateString("en", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function getEngagement(user) {
  return ["likes", "replies", "retweets", "quotes", "bookmarks"].reduce(
    (sum, key) => sum + Number(user[key] || 0),
    0,
  );
}

function getScore(user) {
  return Math.round(
    Number(user.tweet_count || 0) * 100 +
      Number(user.likes || 0) * 8 +
      Number(user.views || 0) * 0.35 +
      Number(user.replies || 0) * 18 +
      Number(user.retweets || 0) * 28 +
      Number(user.quotes || 0) * 24 +
      Number(user.bookmarks || 0) * 12,
  );
}

function normalizeUser(user, index) {
  const author = user.author || {};
  const handle = author.handle || `unknown-${index + 1}`;
  const engagement = getEngagement(user);
  return {
    ...user,
    rankSeed: index + 1,
    score: getScore(user),
    engagement,
    author: {
      id: author.id || "",
      handle,
      name: author.name || handle,
      followers_count: Number(author.followers_count || 0),
      profile_image_url: author.profile_image_url || "/assets/avax-mark.png",
      verified: Boolean(author.verified),
    },
  };
}

function sortValue(user, sortKey) {
  if (sortKey === "score") return user.score;
  if (sortKey === "engagement") return user.engagement;
  return Number(user[sortKey] || 0);
}

function avatarUrl(user) {
  return user.author.profile_image_url || "/assets/avax-mark.png";
}

function AppShell({ children }) {
  return (
    <main className="app-shell">
      <div className="ambient-copy" aria-hidden="true">
        <span>@HotEminSummer</span>
        <span>gHotemin</span>
        <span>$hotemin</span>
        <span>AVAX</span>
      </div>
      {children}
    </main>
  );
}

function StatCard({ label, value, accent }) {
  return (
    <article className={`stat-card ${accent ? "stat-card-accent" : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function LeaderRow({ user, rank, active, onSelect }) {
  const handle = user.author.handle;
  return (
    <button
      className={`leader-row ${active ? "is-active" : ""}`}
      onClick={() => onSelect(user)}
      type="button"
    >
      <span className="rank">{rank}</span>
      <span className="identity">
        <img
          alt=""
          src={avatarUrl(user)}
          onError={(event) => {
            event.currentTarget.src = "/assets/avax-mark.png";
          }}
        />
        <span>
          <strong>@{handle}</strong>
          <small>{user.author.name}</small>
        </span>
      </span>
      <span className="metric">{numberFormat(user.score)}</span>
      <span className="metric">{numberFormat(user.tweet_count)}</span>
      <span className="metric">{numberFormat(user.views)}</span>
      <span className="metric">{numberFormat(user.likes)}</span>
    </button>
  );
}

function AuthorPanel({ user, tweets }) {
  if (!user) {
    return (
      <aside className="author-panel empty-panel">
        <img src="/assets/avax-mark.png" alt="" />
        <p>Select a leader to inspect their Hot Emin footprint.</p>
      </aside>
    );
  }

  const authorTweets = tweets
    .filter((tweet) => (tweet.author?.handle || "").toLowerCase() === user.author.handle.toLowerCase())
    .sort((a, b) => String(b.id || "").localeCompare(String(a.id || "")))
    .slice(0, 4);

  return (
    <aside className="author-panel">
      <div className="panel-head">
        <img
          alt=""
          src={avatarUrl(user)}
          onError={(event) => {
            event.currentTarget.src = "/assets/avax-mark.png";
          }}
        />
        <div>
          <span>Selected author</span>
          <h2>@{user.author.handle}</h2>
          <p>{user.author.name}</p>
        </div>
      </div>

      <div className="panel-score">
        <span>Total score</span>
        <strong>{numberFormat(user.score)}</strong>
      </div>

      <div className="panel-grid">
        {["tweet_count", "views", "likes", "replies", "retweets", "quotes"].map((key) => (
          <div key={key}>
            <span>{metricLabels[key]}</span>
            <strong>{numberFormat(user[key])}</strong>
          </div>
        ))}
      </div>

      <div className="term-stack">
        {(user.matched_terms || []).map((term) => (
          <span key={term}>{term}</span>
        ))}
      </div>

      <div className="timebox">
        <div>
          <span>First post</span>
          <strong>{dateLabel(user.first_tweet_at)}</strong>
        </div>
        <div>
          <span>Latest post</span>
          <strong>{dateLabel(user.last_tweet_at)}</strong>
        </div>
      </div>

      <div className="tweet-list">
        <h3>Recent matched posts</h3>
        {authorTweets.length ? (
          authorTweets.map((tweet) => (
            <a key={tweet.id} href={tweet.url || "#"} target="_blank" rel="noreferrer">
              <span>{dateLabel(tweet.created_at)}</span>
              <p>{tweet.text}</p>
            </a>
          ))
        ) : (
          <p className="muted">No tweet preview available in this JSON export.</p>
        )}
      </div>
    </aside>
  );
}

export function App() {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState("score");
  const [isOpen, setIsOpen] = useState(false);
  const [isMuted, setIsMuted] = useState(false);
  const [selectedHandle, setSelectedHandle] = useState("");
  const audioRef = useRef(null);

  useEffect(() => {
    const audio = new Audio(SONG_URL);
    audio.loop = true;
    audio.preload = "auto";
    audio.volume = 0.72;
    audioRef.current = audio;
    return () => {
      audio.pause();
      audioRef.current = null;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch(DATA_URL)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Could not load ${DATA_URL}`);
        }
        return response.json();
      })
      .then((data) => {
        if (cancelled) return;
        setPayload(data);
        const firstUser = (data.users || [])[0];
        if (firstUser?.author?.handle) {
          setSelectedHandle(firstUser.author.handle);
        }
      })
      .catch((loadError) => {
        if (!cancelled) {
          setError(loadError.message);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const users = useMemo(() => (payload?.users || []).map(normalizeUser), [payload]);
  const tweets = payload?.tweets || [];

  const totals = useMemo(() => {
    const metricTotals = payload?.collection?.metric_totals || {};
    return {
      users: payload?.collection?.user_count || users.length,
      tweets: payload?.collection?.tweet_count || tweets.length,
      views: metricTotals.views || users.reduce((sum, user) => sum + Number(user.views || 0), 0),
      likes: metricTotals.likes || users.reduce((sum, user) => sum + Number(user.likes || 0), 0),
    };
  }, [payload, tweets.length, users]);

  const filteredUsers = useMemo(() => {
    const normalizedQuery = query.trim().replace(/^@/, "").toLowerCase();
    return users
      .filter((user) => {
        if (!normalizedQuery) return true;
        return (
          user.author.handle.toLowerCase().includes(normalizedQuery) ||
          user.author.name.toLowerCase().includes(normalizedQuery)
        );
      })
      .sort((a, b) => sortValue(b, sortKey) - sortValue(a, sortKey));
  }, [query, sortKey, users]);

  const selectedUser = useMemo(() => {
    const exact = users.find(
      (user) => user.author.handle.toLowerCase() === selectedHandle.toLowerCase(),
    );
    return exact || filteredUsers[0] || users[0];
  }, [filteredUsers, selectedHandle, users]);

  useEffect(() => {
    if (filteredUsers.length && !filteredUsers.some((user) => user.author.handle === selectedHandle)) {
      setSelectedHandle(filteredUsers[0].author.handle);
    }
  }, [filteredUsers, selectedHandle]);

  const openLeaderboard = () => {
    setIsOpen(true);
    const audio = audioRef.current;
    if (!audio) return;
    audio.volume = 0.72;
    audio.muted = false;
    setIsMuted(false);
    audio.play().catch(() => {
      setIsMuted(true);
    });
  };

  const toggleSound = () => {
    const audio = audioRef.current;
    if (!audio) return;
    const nextMuted = !audio.muted;
    audio.muted = nextMuted;
    setIsMuted(nextMuted);
    if (!nextMuted && audio.paused) {
      audio.play().catch(() => {
        audio.muted = true;
        setIsMuted(true);
      });
    }
  };

  if (!isOpen) {
    return (
      <AppShell>
        <section className="load-state intro-state">
          <img className="intro-art intro-art-left" src="/assets/glitch-emin-subject.png" alt="" />
          <img className="intro-art intro-art-right" src="/assets/text-orb-subject.png" alt="" />
          <img src="/assets/avax-mark.png" alt="" />
          <h1>Hot Emin Summer Leaderbord</h1>
          <p>{error || (payload ? "Leaderboard data is ready." : "Loading leaderboard data...")}</p>
          <button
            className="open-leaderboard"
            type="button"
            disabled={!payload || Boolean(error)}
            onClick={openLeaderboard}
          >
            Open leaderbord
          </button>
        </section>
      </AppShell>
    );
  }

  if (error) {
    return (
      <AppShell>
        <section className="load-state">
          <img src="/assets/avax-mark.png" alt="" />
          <h1>Hot Emin Summer Leaderbord</h1>
          <p>{error}</p>
        </section>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <header className="hero">
        <div className="brand-lockup">
          <img src="/assets/avax-mark.png" alt="" />
          <span>Hot Emin Summer</span>
        </div>
        <div className="status-pill">SocialData crawl status: {payload.collection?.status || "unknown"}</div>
        <button
          className={`sound-toggle ${isMuted ? "is-muted" : ""}`}
          type="button"
          onClick={toggleSound}
          aria-label={isMuted ? "Turn sound on" : "Mute sound"}
          title={isMuted ? "Turn sound on" : "Mute sound"}
        >
          <span aria-hidden="true">{isMuted ? "Sound off" : "Sound on"}</span>
        </button>
        <div className="hero-copy">
          <h1>Hot Emin Summer Leaderbord</h1>
        </div>
        <div className="hero-media">
          <img src="/assets/hero-banner.png" alt="" />
        </div>
      </header>

      <section className="dashboard-band">
        <div className="summary-grid">
          <StatCard label="Tracked posts" value={numberFormat(totals.tweets)} accent />
          <StatCard label="Authors" value={numberFormat(totals.users)} />
          <StatCard label="Total views" value={numberFormat(totals.views)} />
          <StatCard label="Total likes" value={numberFormat(totals.likes)} />
        </div>

        <div className="workbench">
          <section className="leaderboard">
            <div className="toolbar">
              <label className="search-box">
                <span>Find handle</span>
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="@username"
                />
              </label>

              <label className="sort-box">
                <span>Sort by</span>
                <select value={sortKey} onChange={(event) => setSortKey(event.target.value)}>
                  {SORT_OPTIONS.map((option) => (
                    <option key={option.key} value={option.key}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className="table-shell">
              <div className="leader-header">
                <span>#</span>
                <span>Author</span>
                <span>Score</span>
                <span>Posts</span>
                <span>Views</span>
                <span>Likes</span>
              </div>
              <div className="leader-rows">
                {filteredUsers.slice(0, 100).map((user, index) => (
                  <LeaderRow
                    key={user.author.id || user.author.handle}
                    user={user}
                    rank={index + 1}
                    active={selectedUser?.author.handle === user.author.handle}
                    onSelect={(nextUser) => setSelectedHandle(nextUser.author.handle)}
                  />
                ))}
              </div>
            </div>
          </section>

          <AuthorPanel user={selectedUser} tweets={tweets} />
        </div>
      </section>

      <section className="poster-band">
        <img src="/assets/statue-poster.png" alt="" />
        <div>
          <span>Project pulse</span>
          <h2>Every mention becomes a rankable footprint.</h2>
          <p>
            The leaderboard reads the local SocialData export and recalculates the ranking
            in-browser from posts, views, likes and engagement.
          </p>
        </div>
      </section>
    </AppShell>
  );
}
