package main

import (
	"compress/flate"
	"compress/gzip"
	"crypto/tls"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"regexp"
	"strings"
)

var (
	urlAttrRe = regexp.MustCompile(`((?:href|src|action|srcset|poster|data|formaction)\s*=\s*)(["'])([^"']*?)(["'])`)
	cssURLRe  = regexp.MustCompile(`(url\s*\(\s*)(["']?)([^)"']+)(["']?\s*\))`)
)

func main() {
	server := &http.Server{
		Addr:    ":9999",
		Handler: http.HandlerFunc(handler),
	}
	log.Println("subnet-proxy listening on :9999")
	log.Fatal(server.ListenAndServe())
}

func handler(w http.ResponseWriter, r *http.Request) {
	rawURI := r.RequestURI
	targetRaw := strings.TrimPrefix(rawURI, "/")

	if targetRaw == "" || targetRaw == "favicon.ico" {
		http.Error(w, "Usage: /http://target-host:port/path or /https://target-host:port/path", http.StatusBadRequest)
		return
	}

	if !strings.HasPrefix(targetRaw, "http://") && !strings.HasPrefix(targetRaw, "https://") {
		http.Error(w, "Target URL must start with http:// or https://", http.StatusBadRequest)
		return
	}

	targetURL, err := url.Parse(targetRaw)
	if err != nil {
		http.Error(w, "Invalid target URL: "+err.Error(), http.StatusBadRequest)
		return
	}

	proxyScheme := "http"
	if r.TLS != nil {
		proxyScheme = "https"
	}
	proxyHost := r.Host
	proxyBase := proxyScheme + "://" + proxyHost

	targetBase := targetURL.Scheme + "://" + targetURL.Host

	req, err := http.NewRequestWithContext(r.Context(), r.Method, targetRaw, r.Body)
	if err != nil {
		http.Error(w, "Failed to create request: "+err.Error(), http.StatusInternalServerError)
		return
	}

	for k, vs := range r.Header {
		switch strings.ToLower(k) {
		case "host", "accept-encoding":
			continue
		}
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	req.Header.Set("Host", targetURL.Host)
	req.Header.Set("Accept-Encoding", "gzip, deflate, identity")

	client := &http.Client{
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		},
	}

	resp, err := client.Do(req)
	if err != nil {
		http.Error(w, "Proxy request failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	if loc := resp.Header.Get("Location"); loc != "" {
		rewritten := rewriteURL(loc, targetBase, proxyBase)
		resp.Header.Set("Location", rewritten)
	}

	for _, cookie := range resp.Cookies() {
		cookie.Domain = ""
		cookie.Path = "/" + targetBase + cookie.Path
		http.SetCookie(w, cookie)
	}

	contentType := resp.Header.Get("Content-Type")
	isHTML := strings.Contains(contentType, "text/html")
	isCSS := strings.Contains(contentType, "text/css")

	if isHTML || isCSS {
		body, err := decodeBody(resp)
		if err != nil {
			http.Error(w, "Failed to read response: "+err.Error(), http.StatusBadGateway)
			return
		}

		rewritten := rewriteContent(body, targetBase, proxyBase, isHTML)

		for k, vs := range resp.Header {
			switch strings.ToLower(k) {
			case "content-length", "content-encoding", "transfer-encoding", "set-cookie":
				continue
			}
			for _, v := range vs {
				w.Header().Add(k, v)
			}
		}
		w.Header().Set("Content-Length", fmt.Sprintf("%d", len(rewritten)))
		w.WriteHeader(resp.StatusCode)
		w.Write([]byte(rewritten))
		return
	}

	for k, vs := range resp.Header {
		switch strings.ToLower(k) {
		case "set-cookie":
			continue
		}
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

func decodeBody(resp *http.Response) (string, error) {
	var reader io.Reader
	switch resp.Header.Get("Content-Encoding") {
	case "gzip":
		gr, err := gzip.NewReader(resp.Body)
		if err != nil {
			return "", err
		}
		defer gr.Close()
		reader = gr
	case "deflate":
		reader = flate.NewReader(resp.Body)
	default:
		reader = resp.Body
	}
	data, err := io.ReadAll(reader)
	if err != nil {
		return "", err
	}
	return string(data), nil
}

func rewriteURL(rawURL, targetBase, proxyBase string) string {
	if strings.HasPrefix(rawURL, "http://") || strings.HasPrefix(rawURL, "https://") {
		if strings.HasPrefix(rawURL, targetBase) {
			return proxyBase + "/" + rawURL
		}
		return proxyBase + "/" + rawURL
	}

	if strings.HasPrefix(rawURL, "//") {
		return proxyBase + "/http:" + rawURL
	}

	if strings.HasPrefix(rawURL, "/") {
		return proxyBase + "/" + targetBase + rawURL
	}

	return rawURL
}

func rewriteContent(body, targetBase, proxyBase string, isHTML bool) string {
	body = strings.ReplaceAll(body, targetBase, proxyBase+"/"+targetBase)

	if isHTML {
		body = urlAttrRe.ReplaceAllStringFunc(body, func(match string) string {
			groups := urlAttrRe.FindStringSubmatch(match)
			if len(groups) < 5 {
				return match
			}
			attr, openQ, val, closeQ := groups[1], groups[2], groups[3], groups[4]

			if strings.HasPrefix(val, "data:") || strings.HasPrefix(val, "javascript:") || strings.HasPrefix(val, "mailto:") || strings.HasPrefix(val, "#") {
				return match
			}

			if strings.Contains(val, proxyBase) {
				return match
			}

			if strings.HasPrefix(val, "/") && !strings.HasPrefix(val, "//") && !strings.HasPrefix(val, "/http://") && !strings.HasPrefix(val, "/https://") {
				val = "/" + targetBase + val
				return attr + openQ + val + closeQ
			}

			return match
		})
	}

	body = cssURLRe.ReplaceAllStringFunc(body, func(match string) string {
		groups := cssURLRe.FindStringSubmatch(match)
		if len(groups) < 5 {
			return match
		}
		prefix, openQ, val, suffix := groups[1], groups[2], groups[3], groups[4]

		if strings.HasPrefix(val, "data:") || strings.Contains(val, proxyBase) {
			return match
		}

		if strings.HasPrefix(val, "/") && !strings.HasPrefix(val, "//") && !strings.HasPrefix(val, "/http://") && !strings.HasPrefix(val, "/https://") {
			val = "/" + targetBase + val
			return prefix + openQ + val + suffix
		}

		return match
	})

	return body
}
