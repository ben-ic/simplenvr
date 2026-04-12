import "react";

declare module "react" {
  namespace JSX {
    interface IntrinsicElements {
      "rtsp-tile": React.DetailedHTMLProps<
        React.HTMLAttributes<HTMLElement> & {
          src?: string;
          muted?: boolean | "";
          name?: string;
          status?: string;
          timestamp?: string;
          motion?: boolean | "";
        },
        HTMLElement
      >;
    }
  }
}
