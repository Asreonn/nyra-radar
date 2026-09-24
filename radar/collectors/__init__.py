"""Collector registry: Source.kind -> Collector class."""
from __future__ import annotations

from radar.collectors.base import Collector, CollectorError, FetchResult, MissingCredentials, SourceSpec
from radar.collectors.github import (
                                     GitHubOrgCollector,
                                     GitHubSearchCollector,
                                     GitHubTrendingCollector,
                                     OSSInsightCollector,
)
from radar.collectors.github_releases import GitHubReleasesCollector
from radar.collectors.google_news import GoogleNewsCollector
from radar.collectors.hackernews import HackerNewsCollector, HNSearchCollector
from radar.collectors.reddit import RedditCollector
from radar.collectors.rss import RSSCollector
from radar.collectors.telegram import TelegramCollector
from radar.collectors.website_watch import WebsiteWatchCollector
from radar.collectors.youtube import YouTubeCollector

COLLECTORS: dict[str, type[Collector]] = {
    c.kind: c
    for c in (RSSCollector, HackerNewsCollector, HNSearchCollector, RedditCollector, GitHubSearchCollector,
              GitHubTrendingCollector, OSSInsightCollector, GitHubOrgCollector, GitHubReleasesCollector,
              GoogleNewsCollector, YouTubeCollector, WebsiteWatchCollector, TelegramCollector)
}

__all__ = ["COLLECTORS", "Collector", "CollectorError", "FetchResult", "MissingCredentials", "SourceSpec"]
