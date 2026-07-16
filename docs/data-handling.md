# Atlas OS — Data Handling & Consent

Copyright © 2026 Tyler Buck · AGPL-3.0

*A plain-language statement for the homeowner, to be reviewed and signed
before installation. Not legal advice — have counsel adapt it to your
jurisdiction before commercial use. It reflects what the software
actually does, so that the promise and the code match.*

---

## What Atlas is

Atlas is an operating system that runs **on a computer in your home**.
It watches the sensors and devices you connect to it and can act on them
on your behalf. An optional AI assistant reasons over that information.

## Where your data lives

**On your hardware. Only.** Everything Atlas records — sensor readings,
device history, documents you add, any camera images, and the questions
you ask the assistant — is stored on the computer in your home. Atlas is
built to run with the internet unplugged, and it blocks its own services
from reaching the internet at the network level. There is no Atlas
cloud. Nothing is sold, shared, or sent anywhere.

If an AI assistant is enabled, it runs a model **on your own hardware**.
Your data is never sent to an outside AI service.

## How sensitive information is protected

Atlas sorts everything it knows into four sensitivity levels, and the
most sensitive — presence/occupancy, cameras, microphones, location,
health — is treated specially by the software itself:

- It is readable only by the specific component that captured it and by
  the system operator. Other parts of the system, including the AI, are
  **structurally unable** to read it.
- It is never handed to any AI model without an explicit, recorded
  permission you grant.
- When it does travel inside the system, its content is hidden; only the
  fact that "something changed" is shared.

Every action the system takes is recorded in a log you can inspect, and
every action must pass a rule set that **you** control — the system does
nothing you have not permitted.

## Cameras (if any are installed)

Cameras are only installed where you specifically agree, aimed only
where you agree. By default a camera is not a live stream — it takes a
single image only when an action you or the assistant initiated calls
for it, the image is used to answer that request, and it is discarded
unless you choose to keep it. A visible light indicates when a camera
captures. You may remove any camera, or revoke camera permissions, at
any time.

Cameras in shared or private spaces (bathrooms, bedrooms, guest areas)
deserve extra thought; the installer will discuss placement with you and
will not install a camera you are not comfortable with.

## Who can see and control the system

Control requires a cryptographic credential (the "operator" identity)
held by you and, during setup, your installer. Without it, the system
answers nothing. Your installer will explain who holds this credential
after handover — ideally, you.

## The trust root

Your system has its own private security authority (a "certificate
authority") created on your hardware. Its key is backed up and a sealed
copy is left with you. It is *yours*. It never leaves your possession
except as backups you control.

## Your rights over your data

- **See it:** you (or your installer, at your request) can inspect
  everything the system holds.
- **Export it:** the data is in standard files on your hardware.
- **Delete it:** you can remove any record, any device, or wipe the
  entire system, at any time. A full reset destroys all stored data.
- **Stop it:** unplug it. It is a computer in your home; it has an
  off switch, and it does not depend on anyone else to keep working or
  to be shut down.

## The software's license

Atlas is free and open-source software under the GNU AGPL-3.0. You are
entitled to the source code, to run it, and to have others inspect it.
You are not locked in to any vendor.

## What the installer commits to

- Set up the system as described here.
- Leave you the security credentials and the backup of your trust root.
- Not retain a copy of your data, or remote access to your system,
  beyond what you explicitly ask them to keep for support.
- Discuss and honor your choices about cameras and sensitive sensors.

---

```
Homeowner: ______________________   Date: ____________

Cameras in scope?   Yes / No     If yes, agreed locations:
  __________________________________________________________

Presence/occupancy sensing in scope?   Yes / No

Who holds the operator credential after handover?  ___________
Where is the homeowner's CA backup kept?            ___________

Homeowner signature: ____________________________________
Installer signature: ____________________________________
```
