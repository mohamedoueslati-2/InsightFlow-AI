from __future__ import annotations

from typing import Callable

from ..presentation_catalog import PresentationCatalog


class PresentationToolkit:
    def __init__(self, catalog: PresentationCatalog):
        self.catalog = catalog

    def get_tools(self) -> list[Callable]:
        return [
            self.get_report_overview,
            self.list_report_sections,
            self.get_report_section,
            self.list_tables,
            self.get_table,
            self.list_assets,
            self.get_asset_metadata,
            self.get_closing_guidance,
        ]

    def get_report_overview(self) -> dict:
        """Return report title and counts of sections, extracted Word tables and original visual assets."""
        return self.catalog.overview()

    def list_report_sections(self) -> list[dict]:
        """List extracted report sections with ids, titles, short text previews and associated image ids."""
        return self.catalog.list_sections()

    def get_report_section(self, section_id: str) -> dict:
        """Return the full extracted text and metadata for one real report section id."""
        return self.catalog.get_section(section_id)

    def list_tables(self) -> list[dict]:
        """List real Word tables extracted from the DOCX with ids, dimensions, section provenance and preview rows."""
        return self.catalog.list_tables()

    def get_table(self, table_id: str) -> dict:
        """Return all rows and provenance for one real extracted Word table id."""
        return self.catalog.get_table(table_id)

    def list_assets(self) -> list[dict]:
        """List original embedded Word visuals with chart-title/paragraph association context and stable ids."""
        return self.catalog.list_assets()

    def get_asset_metadata(self, asset_id: str) -> dict:
        """Return rich deterministic association metadata for one original Word asset without modifying it."""
        return self.catalog.get_asset(asset_id)

    def get_closing_guidance(self) -> dict:
        """Return explicit conclusion/recommendation text detected in the extracted report, with source provenance."""
        return self.catalog.get_closing_guidance()
