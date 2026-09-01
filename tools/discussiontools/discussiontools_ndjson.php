<?php
declare( strict_types = 1 );

/*
 * NDJSON adapter around the production PHP parser.  The parsing sequence is
 * intentionally the same one used by HookUtils::parseRevisionParsoidHtml():
 * DOMUtils::parseHTML -> DOMCompat::getBody -> unwrapParsoidSections ->
 * DiscussionTools.CommentParser::parse.
 */

use MediaWiki\Extension\DiscussionTools\CommentUtils;
use MediaWiki\Extension\DiscussionTools\ThreadItem\ContentCommentItem;
use MediaWiki\Extension\DiscussionTools\ThreadItem\ContentHeadingItem;
use MediaWiki\MediaWikiServices;
use MediaWiki\Parser\Sanitizer;
use Wikimedia\Parsoid\Core\DOMCompat;
use Wikimedia\Parsoid\DOM\Element;
use Wikimedia\Parsoid\DOM\Node;
use Wikimedia\Parsoid\Utils\DOMUtils;

$IP = '/opt/mediawiki';
define( 'MEDIAWIKI', true );
define( 'MW_ENTRY_POINT', 'cli' );
require_once "$IP/includes/BootstrapHelperFunctions.php";
require_once "$IP/includes/Setup.php";

/** @var array<string,string> $versions */
$versions = require __DIR__ . '/versions.php';
$versions['parsoid_composer_version'] = installedPackageVersion( "$IP/vendor/composer/installed.json", 'wikimedia/parsoid' );
$versions['php_version'] = PHP_VERSION;
$versions['composer_version'] = trim( (string)file_get_contents( '/opt/composer-version.txt' ) );
$versions['composer_phar_sha256'] = hash_file( 'sha256', '/usr/local/bin/composer' );
$versions['harness_script_sha256'] = hash_file( 'sha256', __FILE__ );
$versions['local_settings_sha256'] = hash_file( 'sha256', "$IP/LocalSettings.php" );
$versions['versions_file_sha256'] = hash_file( 'sha256', __DIR__ . '/versions.php' );

if ( in_array( '--version', $argv, true ) ) {
	echo json_encode( [ 'type' => 'discussiontools_harness_versions', 'versions' => $versions ], JSON_UNESCAPED_SLASHES ) . "\n";
	exit( 0 );
}

$parser = MediaWikiServices::getInstance()->getService( 'DiscussionTools.CommentParser' );
while ( ( $line = fgets( STDIN ) ) !== false ) {
	if ( trim( $line ) === '' ) {
		continue;
	}
	$revisionId = null;
	$titleText = null;
	$html = null;
	try {
		$record = json_decode( $line, true, 512, JSON_THROW_ON_ERROR );
		$revisionId = $record['revision_id'] ?? null;
		$titleText = $record['title'] ?? null;
		$html = $record['html'] ?? null;
		if ( !is_int( $revisionId ) && !is_string( $revisionId ) ) {
			throw new InvalidArgumentException( 'revision_id is required' );
		}
		if ( !is_string( $titleText ) || $titleText === '' ) {
			throw new InvalidArgumentException( 'title is required' );
		}
		if ( !is_string( $html ) ) {
			throw new InvalidArgumentException( 'html is required' );
		}

		$doc = DOMUtils::parseHTML( $html );
		$body = DOMCompat::getBody( $doc );
		CommentUtils::unwrapParsoidSections( $body );
		$title = MediaWikiServices::getInstance()->getTitleParser()->parseTitle( $titleText );
		$items = $parser->parse( $body, $title );
		$comments = [];
		$headings = [];
		foreach ( $items->getThreadItems() as $item ) {
			if ( $item instanceof ContentCommentItem ) {
				$comments[] = commentPayload( $item );
			} elseif ( $item instanceof ContentHeadingItem ) {
				$headings[] = headingPayload( $item );
			}
		}
		echo json_encode( [
			'type' => 'discussiontools_parse',
			'revision_id' => $revisionId,
			'title' => $titleText,
			'html_sha256' => hash( 'sha256', $html ),
			'versions' => $versions,
			'comments' => $comments,
			'headings' => $headings,
		], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE ) . "\n";
	} catch ( Throwable $error ) {
			echo json_encode( [
			'type' => 'discussiontools_error',
			'revision_id' => $revisionId,
			'title' => $titleText,
			'html_sha256' => is_string( $html ) ? hash( 'sha256', $html ) : null,
			'versions' => $versions,
			'error_class' => $error::class,
			'error' => $error->getMessage(),
			'error_trace' => $error->getTraceAsString(),
		], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE ) . "\n";
	}
}

/** @return array<string,mixed> */
function commentPayload( ContentCommentItem $comment ): array {
	return array_merge( itemPayload( $comment ), [
		'author' => $comment->getAuthor(),
		'display_name' => $comment->getDisplayName(),
		'timestamp' => $comment->getTimestamp()->format( DATE_ATOM ),
		// Keep the original rendered timestamp separately. The normalized parser
		// timestamp is useful for ordering but is not a raw-signature anchor.
		'timestamp_text' => rangeText( $comment->getTimestampRanges()[0] ),
		'signature_text' => rangeText( $comment->getSignatureRanges()[0] ),
		'text' => $comment->getBodyText( true ),
		'html' => $comment->getBodyHTML( true ),
		'signature_ranges' => array_map( 'rangePayload', $comment->getSignatureRanges() ),
		'timestamp_ranges' => array_map( 'rangePayload', $comment->getTimestampRanges() ),
		'body_range' => rangePayload( $comment->getBodyRange() ),
	] );
}

/** @return array<string,mixed> */
function headingPayload( ContentHeadingItem $heading ): array {
	return array_merge( itemPayload( $heading ), [
		'heading_level' => $heading->getHeadingLevel(),
		'linkable_id' => $heading->getLinkableId(),
		'text' => $heading->getText(),
		'html' => $heading->getHTML(),
	] );
}

/** @return array<string,mixed> */
function itemPayload( object $item ): array {
	$parent = $item->getParent();
	return [
		'id' => $item->getId(),
		'name' => $item->getName(),
		'level' => $item->getLevel(),
		'parent_id' => $parent ? $parent->getId() : null,
		'warnings' => $item->getWarnings(),
		'range' => rangePayload( $item->getRange() ),
	];
}

/** @return array<string,mixed> */
function rangePayload( object $range ): array {
	return [
		'start' => boundaryPayload( $range->startContainer, $range->startOffset ),
		'end' => boundaryPayload( $range->endContainer, $range->endOffset ),
	];
}

function rangeText( object $range ): string {
	return Sanitizer::stripAllTags( DOMUtils::getFragmentInnerHTML( $range->cloneContents() ) );
}

/** @return array<string,mixed> */
function boundaryPayload( Node $node, int $offset ): array {
	return [ 'dom_path' => domPath( $node ), 'offset' => $offset, 'node' => $node->nodeName ];
}

/** @return list<int> */
function domPath( Node $node ): array {
	$path = [];
	while ( $node->parentNode ) {
		$index = 0;
		for ( $sibling = $node->previousSibling; $sibling; $sibling = $sibling->previousSibling ) {
			$index++;
		}
		array_unshift( $path, $index );
		$node = $node->parentNode;
	}
	return $path;
}

function installedPackageVersion( string $installedPath, string $package ): ?string {
	if ( !is_file( $installedPath ) ) {
		return null;
	}
	$data = json_decode( file_get_contents( $installedPath ), true );
	$packages = $data['packages'] ?? $data;
	foreach ( $packages as $installed ) {
		if ( ( $installed['name'] ?? null ) === $package ) {
			return $installed['pretty_version'] ?? $installed['version'] ?? null;
		}
	}
	return null;
}
