<?php
// Minimal settings for CLI-only construction of MediaWikiServices.  No request,
// web server, schema installation, or database access is performed by the
// harness.  The parser is given historical Parsoid HTML, not wikitext.

$wgSitename = 'DiscussionTools harness';
$wgScriptPath = '';
$wgArticlePath = '/wiki/$1';
$wgServer = 'https://example.invalid';
$wgLanguageCode = 'en';
$wgLocaltimezone = 'UTC';
$wgDBtype = 'sqlite';
$wgDBname = 'discussiontools_harness_unused';
$wgDBserver = 'localhost';
$wgSQLiteDataDir = '/tmp';
$discussionToolsDatabase = new PDO( "sqlite:$wgSQLiteDataDir/$wgDBname.sqlite" );
$discussionToolsDatabase->exec( <<<'SQL'
CREATE TABLE IF NOT EXISTS interwiki (
    iw_prefix TEXT NOT NULL PRIMARY KEY,
    iw_url BLOB NOT NULL,
    iw_api BLOB NOT NULL DEFAULT '',
    iw_wikiid TEXT NOT NULL DEFAULT '',
    iw_local INTEGER NOT NULL DEFAULT 0,
    iw_trans INTEGER NOT NULL DEFAULT 0
)
SQL );
$discussionToolsDatabase = null;
$wgMainCacheType = CACHE_NONE;
$wgMessageCacheType = CACHE_NONE;
$wgUseDatabaseMessages = false;
$wgCacheDirectory = '/tmp';
$wgShowExceptionDetails = true;

wfLoadExtension( 'VisualEditor' );
wfLoadExtension( 'Linter' );
wfLoadExtension( 'DiscussionTools' );
