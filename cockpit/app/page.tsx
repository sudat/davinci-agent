import { redirect } from "next/navigation";

/** The cockpit opens on the intake screen. */
export default function Home() {
  redirect("/new-episode");
}
