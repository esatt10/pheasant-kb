import type { SourceEditorProps, UiExtension } from "@pheasant/ui/extensions";
import { GtmPage } from "./GtmPage";
import { GtmRegistration } from "./GtmRegistration";
import { GtmResearchDocuments } from "./GtmResearchDocuments";
import { gtmLocation, isGtmCatalog } from "./GtmCatalog";
import "./styles.css";

function SourceEditor({ source, onClose }: SourceEditorProps) {
  return source && isGtmCatalog(source)
    ? <GtmRegistration catalogName={source.name} onClose={onClose} />
    : <GtmRegistration source={source} onClose={onClose} />;
}

export default {
  id: "gtm",
  apiVersion: 1,
  routes: [{ path: "/gtm", label: "GTM", Page: GtmPage }],
  sources: [{
    type: "gtm_research",
    addLabel: "+ GTM research",
    Editor: SourceEditor,
    FolderDetails: GtmResearchDocuments,
    appearance: (source) => isGtmCatalog(source) ? {
      label: "GTM catalog",
      pathLabel: "Public web",
      editLabel: "Add company",
      browseLabel: "Taxonomy",
      outline: false,
      link: { label: "Companies", to: `${gtmLocation(source.name).pathname}${gtmLocation(source.name).search}` },
    } : { label: "GTM research", pathLabel: "Public web" },
  }],
  graphColors: { gtm_evidence: "#5c767a" },
} satisfies UiExtension;
