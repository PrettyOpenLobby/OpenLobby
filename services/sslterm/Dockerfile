# sslterm: SSL 3.0 terminator for the PlayOnline clients' https: fetches.
#
# The handshake and the certificate chain are patapancakes/sslspoof
# (AGPL-3.0), built unmodified from upstream at a pinned commit and pulled in
# through a go.mod replace. Nothing of it is copied into this repo: bump
# SSLSPOOF_REF to update. main.go is the relay and the handshake log.
#
# Upstream: https://github.com/patapancakes/sslspoof
FROM golang:1.27-alpine AS build
RUN apk add --no-cache git
ARG SSLSPOOF_REF=31425aa
RUN git clone https://github.com/patapancakes/sslspoof /sslspoof \
 && git -C /sslspoof checkout --quiet "$SSLSPOOF_REF"
WORKDIR /src
COPY go.mod *.go ./
RUN go mod edit -replace github.com/patapancakes/sslspoof=/sslspoof \
 && go vet . && go test . \
 && CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /sslterm .

FROM alpine:3.22
COPY --from=build /sslterm /usr/local/bin/sslterm
ENTRYPOINT ["/usr/local/bin/sslterm"]
